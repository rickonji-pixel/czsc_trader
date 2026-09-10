"""Causal first-hour signed-volume event census for ETF research."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class VolumeImbalanceCensusResult:
    """Pre-return first-hour features, selected events and density evidence."""

    features: pd.DataFrame
    events: pd.DataFrame
    density: dict[str, object]


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


def census_first_hour_volume_imbalance(
    one_minute: pd.DataFrame,
    *,
    evaluation_start: pd.Timestamp | str,
    observation_start_clock: str,
    observation_end_clock: str,
    expected_bars: int,
    threshold_lookback_sessions: int,
    threshold_quantile: float,
    threshold_lag_sessions: int,
    density_window_sessions: int,
    median_events_required: int,
    p10_events_required: int,
    distinct_event_days_required: int,
) -> VolumeImbalanceCensusResult:
    """Select one causal signed-volume imbalance event per eligible day."""

    required = {"Date", "Open", "Close", "Volume"}
    missing = sorted(required.difference(one_minute.columns))
    if missing:
        raise ValueError(f"1m data missing columns {missing}")
    if expected_bars <= 0 or threshold_lookback_sessions <= 0:
        raise ValueError("bar count and threshold history must be positive")
    if threshold_lag_sessions < 1 or not 0 < threshold_quantile < 1:
        raise ValueError("threshold requires at least one lag and a valid quantile")

    bars = one_minute.loc[:, ["Date", "Open", "Close", "Volume"]].copy()
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
        raise ValueError(f"first-hour bar count differs: {invalid.head().to_dict()}")
    observed["minute_direction"] = np.sign(observed["Close"] - observed["Open"])
    observed["signed_volume"] = observed["minute_direction"] * observed["Volume"]
    grouped = observed.groupby("trade_date", sort=True, observed=True)
    features = pd.DataFrame(index=sizes.index)
    features.index.name = "trade_date"
    features["event_time"] = grouped["Date"].last()
    features["first_hour_open"] = grouped["Open"].first().astype(float)
    features["first_hour_close"] = grouped["Close"].last().astype(float)
    features["first_hour_return"] = features["first_hour_close"].div(
        features["first_hour_open"]
    ).sub(1.0)
    features["signed_volume_imbalance"] = grouped["signed_volume"].sum().div(
        grouped["Volume"].sum()
    )
    features["imbalance_magnitude"] = features["signed_volume_imbalance"].abs()
    features["imbalance_threshold"] = (
        features["imbalance_magnitude"]
        .shift(threshold_lag_sessions)
        .rolling(
            threshold_lookback_sessions,
            min_periods=threshold_lookback_sessions,
        )
        .quantile(threshold_quantile)
    )
    features["pressure_side"] = features["signed_volume_imbalance"].ge(0).map(
        {True: "buy_pressure", False: "sell_pressure"}
    )
    features["price_direction_agrees"] = np.sign(features["first_hour_return"]).eq(
        np.sign(features["signed_volume_imbalance"])
    )
    features["is_volume_imbalance_event"] = (
        features["imbalance_threshold"].notna()
        & features["imbalance_magnitude"].ge(features["imbalance_threshold"])
    )

    start = pd.Timestamp(evaluation_start).normalize()
    features = features.loc[features.index >= start].reset_index()
    calendar = pd.DatetimeIndex(features["trade_date"], name="trade_date")
    events = features.loc[features["is_volume_imbalance_event"]].copy().reset_index(drop=True)
    if events["trade_date"].duplicated().any():
        raise ValueError("volume imbalance census generated more than one event per day")
    median_count, p10_count, minimum_count, maximum_count = _rolling_density(
        pd.DatetimeIndex(events["trade_date"]), calendar, density_window_sessions
    )
    distinct_days = int(events["trade_date"].nunique())
    density_pass = bool(
        median_count >= median_events_required
        and p10_count >= p10_events_required
        and distinct_days >= distinct_event_days_required
    )
    density = {
        "calendar_sessions": int(len(calendar)),
        "independent_events": int(len(events)),
        "distinct_event_days": distinct_days,
        "buy_pressure_events": int(events["pressure_side"].eq("buy_pressure").sum()),
        "sell_pressure_events": int(events["pressure_side"].eq("sell_pressure").sum()),
        "price_direction_agreement": float(events["price_direction_agrees"].mean()),
        "rolling_window_sessions": int(density_window_sessions),
        "rolling_median_events": median_count,
        "rolling_p10_events": p10_count,
        "rolling_min_events": minimum_count,
        "rolling_max_events": maximum_count,
        "density_pass": density_pass,
        "evidence": "DENSITY_CAPABLE" if density_pass else "EVIDENCE_RATE_FAIL",
    }
    return VolumeImbalanceCensusResult(features, events, density)
