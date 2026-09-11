"""Causal opening-shock event census for intraday ETF research."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class OpeningShockCensusResult:
    """Pre-return opening features, selected events and density evidence."""

    features: pd.DataFrame
    events: pd.DataFrame
    density: dict[str, object]


def _rolling_density(
    event_dates: pd.DatetimeIndex,
    calendar: pd.DatetimeIndex,
    window_sessions: int,
) -> tuple[float, float, float, float]:
    counts = pd.Series(0, index=calendar, dtype="int64")
    counts.loc[counts.index.intersection(event_dates)] = 1
    rolling = counts.rolling(window_sessions, min_periods=window_sessions).sum().dropna()
    if rolling.empty:
        return 0.0, 0.0, 0.0, 0.0
    return (
        float(rolling.median()),
        float(rolling.quantile(0.10, interpolation="lower")),
        float(rolling.min()),
        float(rolling.max()),
    )


def census_opening_shocks(
    fifteen_minute: pd.DataFrame,
    *,
    evaluation_start: pd.Timestamp | str,
    threshold_lookback_sessions: int,
    threshold_quantile: float,
    threshold_lag_sessions: int,
    volume_ratio_lookback_sessions: int,
    density_window_sessions: int,
    median_events_required: int,
    p10_events_required: int,
    distinct_event_days_required: int,
) -> OpeningShockCensusResult:
    """Select one causal 09:45 opening-shock event per eligible trading day."""

    required = {"Date", "Close", "Volume"}
    missing = sorted(required.difference(fifteen_minute.columns))
    if missing:
        raise ValueError(f"15m data missing columns {missing}")
    if threshold_lookback_sessions <= 0 or threshold_lag_sessions < 1:
        raise ValueError("opening threshold requires positive history and at least one lag")
    if not 0 < threshold_quantile < 1:
        raise ValueError("threshold quantile must be between zero and one")
    if volume_ratio_lookback_sessions <= 0 or density_window_sessions <= 0:
        raise ValueError("lookback and density windows must be positive")

    bars = fifteen_minute.loc[:, ["Date", "Close", "Volume"]].copy()
    bars["Date"] = pd.to_datetime(bars["Date"], errors="raise")
    bars = bars.sort_values("Date").reset_index(drop=True)
    bars["trade_date"] = bars["Date"].dt.normalize()
    first = bars.groupby("trade_date", sort=True, observed=True).first()
    last = bars.groupby("trade_date", sort=True, observed=True).last()
    if not first.index.equals(last.index):
        raise ValueError("first and last 15m bars cover different dates")

    features = pd.DataFrame(index=first.index)
    features.index.name = "trade_date"
    features["event_time"] = first["Date"]
    features["previous_close"] = last["Close"].shift(1).astype(float)
    features["opening_close"] = first["Close"].astype(float)
    features["opening_return"] = features["opening_close"].div(
        features["previous_close"]
    ).sub(1.0)
    features["opening_magnitude"] = features["opening_return"].abs()
    features["shock_threshold"] = (
        features["opening_magnitude"]
        .shift(threshold_lag_sessions)
        .rolling(
            threshold_lookback_sessions,
            min_periods=threshold_lookback_sessions,
        )
        .quantile(threshold_quantile)
    )
    historical_volume = (
        first["Volume"]
        .astype(float)
        .shift(1)
        .rolling(
            volume_ratio_lookback_sessions,
            min_periods=volume_ratio_lookback_sessions,
        )
        .median()
    )
    features["opening_volume"] = first["Volume"].astype(float)
    features["opening_volume_ratio"] = features["opening_volume"].div(
        historical_volume
    )
    features["shock_side"] = features["opening_return"].ge(0).map(
        {True: "up", False: "down"}
    )
    features["is_opening_shock"] = (
        features["shock_threshold"].notna()
        & features["opening_magnitude"].ge(features["shock_threshold"])
    )

    start = pd.Timestamp(evaluation_start).normalize()
    features = features.loc[features.index >= start].reset_index()
    calendar = pd.DatetimeIndex(features["trade_date"], name="trade_date")
    events = features.loc[features["is_opening_shock"]].copy().reset_index(drop=True)
    if events["trade_date"].duplicated().any():
        raise ValueError("opening census generated more than one event per day")
    median_count, p10_count, minimum_count, maximum_count = _rolling_density(
        pd.DatetimeIndex(events["trade_date"]),
        calendar,
        density_window_sessions,
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
        "up_events": int(events["shock_side"].eq("up").sum()),
        "down_events": int(events["shock_side"].eq("down").sum()),
        "rolling_window_sessions": int(density_window_sessions),
        "rolling_median_events": median_count,
        "rolling_p10_events": p10_count,
        "rolling_min_events": minimum_count,
        "rolling_max_events": maximum_count,
        "density_pass": density_pass,
        "evidence": "DENSITY_CAPABLE" if density_pass else "EVIDENCE_RATE_FAIL",
    }
    return OpeningShockCensusResult(features, events, density)
