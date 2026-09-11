"""Causal event census for pre-registered medium-frequency intraday mechanisms."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class MediumFrequencyCensusResult:
    """Daily pre-return features, mechanism events and density evidence."""

    features: pd.DataFrame
    events: pd.DataFrame
    density: pd.DataFrame


def _rolling_density(
    event_dates: pd.DatetimeIndex,
    calendar: pd.DatetimeIndex,
    window_sessions: int,
) -> tuple[float, float, float, float]:
    flags = pd.Series(0, index=calendar, dtype="int64")
    flags.loc[flags.index.intersection(event_dates)] = 1
    rolling = flags.rolling(window_sessions, min_periods=window_sessions).sum().dropna()
    if rolling.empty:
        return 0.0, 0.0, 0.0, 0.0
    return (
        float(rolling.median()),
        float(rolling.quantile(0.10, interpolation="lower")),
        float(rolling.min()),
        float(rolling.max()),
    )


def census_medium_frequency_mechanisms(
    one_minute: pd.DataFrame,
    *,
    evaluation_start: pd.Timestamp | str,
    observation_start_clock: str,
    observation_end_clock: str,
    expected_bars: int,
    threshold_lookback_sessions: int,
    threshold_quantile: float,
    threshold_lag_sessions: int,
    activity_baseline_sessions: int,
    density_window_sessions: int,
    target_median_min: int,
    target_median_max: int,
    median_events_required: int,
    p10_events_required: int,
    distinct_event_days_required: int,
) -> MediumFrequencyCensusResult:
    """Build three fixed 10:30 event families without reading later prices."""

    required = {"Date", "Open", "High", "Low", "Close", "Volume"}
    missing = sorted(required.difference(one_minute.columns))
    if missing:
        raise ValueError(f"1m data missing columns {missing}")
    if expected_bars <= 0 or threshold_lookback_sessions <= 0:
        raise ValueError("bar count and threshold history must be positive")
    if threshold_lag_sessions < 1 or not 0 < threshold_quantile < 1:
        raise ValueError("threshold requires at least one lag and a valid quantile")

    bars = one_minute.loc[:, sorted(required)].copy()
    bars["Date"] = pd.to_datetime(bars["Date"], errors="raise")
    bars = bars.sort_values("Date").reset_index(drop=True)
    bars["trade_date"] = bars["Date"].dt.normalize()
    bars["clock"] = bars["Date"].dt.strftime("%H:%M")
    observed = bars.loc[
        bars["clock"].between(observation_start_clock, observation_end_clock, inclusive="both")
    ].copy()
    sizes = observed.groupby("trade_date", sort=True, observed=True).size()
    if not sizes.eq(expected_bars).all():
        invalid = sizes.loc[~sizes.eq(expected_bars)]
        raise ValueError(f"observation bar count differs: {invalid.head().to_dict()}")
    observed["typical_price"] = (
        observed["Open"] + observed["High"] + observed["Low"] + observed["Close"]
    ) / 4.0
    observed["weighted_typical"] = observed["typical_price"] * observed["Volume"]
    grouped = observed.groupby("trade_date", sort=True, observed=True)
    features = pd.DataFrame(index=sizes.index)
    features.index.name = "trade_date"
    features["event_time"] = grouped["Date"].last()
    features["first_hour_open"] = grouped["Open"].first().astype(float)
    features["first_hour_close"] = grouped["Close"].last().astype(float)
    features["first_hour_return"] = features["first_hour_close"].div(
        features["first_hour_open"]
    ).sub(1.0)
    features["first_hour_vwap"] = grouped["weighted_typical"].sum().div(
        grouped["Volume"].sum()
    )
    features["vwap_deviation"] = features["first_hour_close"].div(
        features["first_hour_vwap"]
    ).sub(1.0)
    first_bar_return = observed["Close"].div(observed["Open"]).sub(1.0)
    later_return = grouped["Close"].pct_change(fill_method=None)
    observed["path_return"] = later_return.fillna(first_bar_return)
    path_variation = observed["path_return"].abs().groupby(observed["trade_date"]).sum()
    features["path_efficiency"] = features["first_hour_return"].abs().div(
        path_variation.replace(0.0, np.nan)
    )
    features["directional_efficiency_score"] = (
        features["first_hour_return"].abs() * features["path_efficiency"]
    )
    features["first_hour_volume"] = grouped["Volume"].sum().astype(float)
    features["activity_ratio"] = features["first_hour_volume"].div(
        features["first_hour_volume"]
        .shift(1)
        .rolling(activity_baseline_sessions, min_periods=activity_baseline_sessions)
        .median()
    )

    definitions = {
        "VWAP_DEVIATION_REVERSION": ("vwap_deviation", "vwap_deviation", -1),
        "DIRECTIONAL_EFFICIENCY_CONTINUATION": (
            "directional_efficiency_score",
            "first_hour_return",
            1,
        ),
        "ABNORMAL_ACTIVITY_CONTINUATION": ("activity_ratio", "first_hour_return", 1),
    }
    event_frames: list[pd.DataFrame] = []
    for mechanism_id, (score_column, direction_column, multiplier) in definitions.items():
        score = features[score_column].abs()
        threshold = (
            score.shift(threshold_lag_sessions)
            .rolling(threshold_lookback_sessions, min_periods=threshold_lookback_sessions)
            .quantile(threshold_quantile)
        )
        event = threshold.notna() & score.ge(threshold) & features[direction_column].ne(0)
        features[f"{mechanism_id}_score"] = score
        features[f"{mechanism_id}_threshold"] = threshold
        features[f"{mechanism_id}_event"] = event
        selected = features.loc[event].copy().reset_index()
        selected.insert(0, "mechanism_id", mechanism_id)
        selected["direction"] = (
            np.sign(selected[direction_column]).astype(int) * multiplier
        )
        event_frames.append(selected)

    start = pd.Timestamp(evaluation_start).normalize()
    features = features.loc[features.index >= start].reset_index()
    events = pd.concat(event_frames, ignore_index=True)
    events = events.loc[events["trade_date"] >= start].reset_index(drop=True)
    if events.duplicated(["mechanism_id", "trade_date"]).any():
        raise ValueError("a mechanism generated more than one event per day")
    calendar = pd.DatetimeIndex(features["trade_date"], name="trade_date")
    rows: list[dict[str, object]] = []
    for mechanism_id in sorted(definitions):
        selected = events.loc[events["mechanism_id"].eq(mechanism_id)]
        median_count, p10_count, minimum_count, maximum_count = _rolling_density(
            pd.DatetimeIndex(selected["trade_date"]), calendar, density_window_sessions
        )
        distinct_days = int(selected["trade_date"].nunique())
        capable = bool(
            median_count >= median_events_required
            and p10_count >= p10_events_required
            and distinct_days >= distinct_event_days_required
        )
        rows.append(
            {
                "mechanism_id": mechanism_id,
                "events": int(len(selected)),
                "distinct_event_days": distinct_days,
                "long_events": int(selected["direction"].eq(1).sum()),
                "short_overlay_events": int(selected["direction"].eq(-1).sum()),
                "rolling_window_sessions": int(density_window_sessions),
                "rolling_median_events": median_count,
                "rolling_p10_events": p10_count,
                "rolling_min_events": minimum_count,
                "rolling_max_events": maximum_count,
                "inside_target_band": bool(
                    target_median_min <= median_count <= target_median_max
                ),
                "density_pass": capable,
                "evidence": "DENSITY_CAPABLE" if capable else "EVIDENCE_RATE_FAIL",
            }
        )
    return MediumFrequencyCensusResult(features, events, pd.DataFrame(rows))
