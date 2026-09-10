"""Causal relative-style features and event-density evidence."""

from __future__ import annotations

import numpy as np
import pandas as pd


TRADE_SYMBOL = "510500.SH"
REFERENCE_SYMBOLS = ("510300.SH", "512100.SH")
MECHANISMS = (
    "RELATIVE_STRENGTH_CONTINUATION",
    "RELATIVE_RESIDUAL_REVERSION",
    "OPENING_GAP_REVERSION",
)


def build_relative_style_features(panel: pd.DataFrame) -> pd.DataFrame:
    """Build features known by each close/open without any forward return columns."""

    required = {
        "dt", "symbol", "open", "close", "amount", "observed", "staleness_sessions"
    }
    missing = sorted(required.difference(panel.columns))
    if missing:
        raise ValueError(f"aligned panel missing columns {missing}")
    frame = panel.copy()
    frame["dt"] = pd.to_datetime(frame["dt"]).dt.normalize()
    symbols = {TRADE_SYMBOL, *REFERENCE_SYMBOLS}
    if set(frame["symbol"]) != symbols:
        raise ValueError("aligned panel must contain the frozen three-symbol universe")

    close = frame.pivot(index="dt", columns="symbol", values="close").sort_index()
    opening = frame.pivot(index="dt", columns="symbol", values="open").reindex(close.index)
    amount = frame.pivot(index="dt", columns="symbol", values="amount").reindex(close.index)
    observed = (
        frame.pivot(index="dt", columns="symbol", values="observed")
        .reindex(close.index)
        .astype(bool)
    )
    if close.isna().any().any() or opening.isna().any().any():
        raise ValueError("aligned prices must be complete")

    log_close = np.log(close.astype(float))
    reference_log_close = log_close.loc[:, REFERENCE_SYMBOLS].mean(axis=1)
    relative_level = log_close[TRADE_SYMBOL] - reference_log_close
    one_day_residual = relative_level.diff()
    relative_strength_3 = one_day_residual.rolling(3, min_periods=3).sum()
    relative_strength_5 = one_day_residual.rolling(5, min_periods=5).sum()
    relative_strength_20 = one_day_residual.rolling(20, min_periods=20).sum()
    relative_acceleration = relative_strength_5 / 5.0 - relative_strength_20 / 20.0

    log_gap = np.log(opening.astype(float) / close.shift(1))
    opening_gap_residual = log_gap[TRADE_SYMBOL] - log_gap.loc[:, REFERENCE_SYMBOLS].mean(axis=1)
    prior_amount_median = amount.shift(1).rolling(20, min_periods=20).median()
    amount_ratio = (amount / prior_amount_median).where(amount.gt(0) & prior_amount_median.gt(0))
    relative_amount_impulse = np.log(amount_ratio[TRADE_SYMBOL]) - np.log(
        amount_ratio.loc[:, REFERENCE_SYMBOLS]
    ).mean(axis=1)

    clean_today = observed.all(axis=1)
    clean_20 = observed.astype(int).rolling(21, min_periods=21).min().all(axis=1)
    return pd.DataFrame(
        {
            "dt": close.index,
            "relative_residual_1": one_day_residual,
            "relative_strength_3": relative_strength_3,
            "relative_strength_5": relative_strength_5,
            "relative_strength_20": relative_strength_20,
            "relative_acceleration": relative_acceleration,
            "opening_gap_residual": opening_gap_residual,
            "relative_amount_impulse": relative_amount_impulse.replace([np.inf, -np.inf], np.nan),
            "all_symbols_observed": clean_today,
            "clean_20_session_window": clean_20,
        }
    ).reset_index(drop=True)


def _prior_quantile(series: pd.Series, quantile: float, lookback: int) -> pd.Series:
    return series.shift(1).rolling(lookback, min_periods=lookback).quantile(quantile)


def generate_relative_style_events(
    features: pd.DataFrame,
    *,
    evaluation_start: str | pd.Timestamp,
    threshold_lookback: int = 60,
    tail_quantile: float = 0.25,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Generate frozen events and density/overlap evidence without returns."""

    frame = features.copy()
    frame["dt"] = pd.to_datetime(frame["dt"]).dt.normalize()
    frame = frame.sort_values("dt").reset_index(drop=True)
    calendar = pd.DatetimeIndex(frame["dt"])
    next_session = pd.Series(calendar, index=calendar).shift(-1)
    specs = {
        "RELATIVE_STRENGTH_CONTINUATION": {
            "column": "relative_strength_5",
            "tail": "upper",
            "clock": "15:00",
            "execution": "NEXT_OPEN",
            "clean": "clean_20_session_window",
        },
        "RELATIVE_RESIDUAL_REVERSION": {
            "column": "relative_strength_3",
            "tail": "lower",
            "clock": "15:00",
            "execution": "NEXT_OPEN",
            "clean": "clean_20_session_window",
        },
        "OPENING_GAP_REVERSION": {
            "column": "opening_gap_residual",
            "tail": "lower",
            "clock": "09:30",
            "execution": "09:40",
            "clean": "all_symbols_observed",
        },
    }
    event_frames: list[pd.DataFrame] = []
    activation = pd.DataFrame(index=calendar)
    for mechanism, spec in specs.items():
        score = frame[spec["column"]].astype(float)
        threshold = _prior_quantile(
            score,
            1.0 - tail_quantile if spec["tail"] == "upper" else tail_quantile,
            threshold_lookback,
        )
        active = score.gt(threshold) if spec["tail"] == "upper" else score.lt(threshold)
        active &= frame[spec["clean"]].astype(bool)
        signal_dates = calendar[active.fillna(False)]
        if spec["execution"] == "NEXT_OPEN":
            event_dates = next_session.reindex(signal_dates).to_numpy()
        else:
            event_dates = signal_dates.to_numpy()
        events = pd.DataFrame(
            {
                "mechanism": mechanism,
                "signal_date": signal_dates,
                "event_date": event_dates,
                "signal_clock": spec["clock"],
                "execution_clock": spec["execution"],
                "score": score.loc[active.fillna(False)].to_numpy(),
                "threshold": threshold.loc[active.fillna(False)].to_numpy(),
                "tail": spec["tail"],
            }
        ).dropna(subset=["event_date"])
        events = events.loc[events["event_date"] >= pd.Timestamp(evaluation_start)]
        event_frames.append(events)
        activation[mechanism] = calendar.isin(pd.to_datetime(events["event_date"]))

    all_events = pd.concat(event_frames, ignore_index=True).sort_values(
        ["event_date", "mechanism"]
    )
    evaluation_mask = activation.index >= pd.Timestamp(evaluation_start)
    density_rows = []
    annual_rows = []
    for mechanism in MECHANISMS:
        rolling = activation[mechanism].astype(int).rolling(60, min_periods=60).sum()
        eligible = rolling.loc[evaluation_mask].dropna()
        events = all_events.loc[all_events["mechanism"].eq(mechanism)]
        median = float(eligible.median())
        p10 = float(eligible.quantile(0.10))
        density_rows.append(
            {
                "mechanism": mechanism,
                "event_count": int(len(events)),
                "rolling_60_median": median,
                "rolling_60_p10": p10,
                "rolling_60_min": float(eligible.min()),
                "density_eligible": bool(12 <= median <= 20 and p10 >= 8),
            }
        )
        counts = events.assign(year=pd.to_datetime(events["event_date"]).dt.year).groupby("year").size()
        for year in range(pd.Timestamp(evaluation_start).year, activation.index.max().year + 1):
            annual_rows.append(
                {"mechanism": mechanism, "year": year, "event_count": int(counts.get(year, 0))}
            )

    overlap = pd.DataFrame(index=MECHANISMS, columns=MECHANISMS, dtype=float)
    for left in MECHANISMS:
        for right in MECHANISMS:
            left_active = activation[left] & evaluation_mask
            right_active = activation[right] & evaluation_mask
            union = (left_active | right_active).sum()
            overlap.loc[left, right] = float((left_active & right_active).sum() / union) if union else 0.0
    return (
        all_events.reset_index(drop=True),
        pd.DataFrame(density_rows),
        pd.DataFrame(annual_rows),
        overlap,
    )


def generate_price_volume_confirmation_events(
    features: pd.DataFrame,
    *,
    evaluation_start: str | pd.Timestamp,
    normalization_lookback: int = 60,
    threshold_lookback: int = 60,
    tail_quantile: float = 0.25,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Generate one fixed price-volume continuation mechanism without returns."""

    frame = features.copy()
    frame["dt"] = pd.to_datetime(frame["dt"]).dt.normalize()
    frame = frame.sort_values("dt").reset_index(drop=True)
    strength = frame["relative_strength_5"].astype(float)
    amount = frame["relative_amount_impulse"].astype(float)

    def causal_zscore(series: pd.Series) -> pd.Series:
        prior = series.shift(1).rolling(normalization_lookback, min_periods=normalization_lookback)
        mean = prior.mean()
        standard_deviation = prior.std(ddof=0).replace(0.0, float("nan"))
        return (series - mean) / standard_deviation

    strength_z = causal_zscore(strength)
    amount_z = causal_zscore(amount)
    composite = 0.5 * strength_z + 0.5 * amount_z
    threshold = _prior_quantile(composite, 1.0 - tail_quantile, threshold_lookback)
    active = (
        composite.gt(threshold)
        & strength.gt(0)
        & frame["clean_20_session_window"].astype(bool)
    ).fillna(False)
    calendar = pd.DatetimeIndex(frame["dt"])
    next_session = pd.Series(calendar, index=calendar).shift(-1)
    signal_dates = calendar[active]
    events = pd.DataFrame(
        {
            "mechanism": "PRICE_VOLUME_ROTATION_CONFIRMATION",
            "signal_date": signal_dates,
            "event_date": next_session.reindex(signal_dates).to_numpy(),
            "signal_clock": "15:00",
            "execution_clock": "NEXT_OPEN",
            "relative_strength_5": strength.loc[active].to_numpy(),
            "relative_amount_impulse": amount.loc[active].to_numpy(),
            "composite_score": composite.loc[active].to_numpy(),
            "threshold": threshold.loc[active].to_numpy(),
        }
    ).dropna(subset=["event_date"])
    events = events.loc[events["event_date"] >= pd.Timestamp(evaluation_start)].reset_index(drop=True)

    activation = pd.Series(calendar.isin(pd.to_datetime(events["event_date"])), index=calendar)
    rolling = activation.astype(int).rolling(60, min_periods=60).sum()
    eligible_rolling = rolling.loc[rolling.index >= pd.Timestamp(evaluation_start)].dropna()
    density = pd.DataFrame(
        [
            {
                "mechanism": "PRICE_VOLUME_ROTATION_CONFIRMATION",
                "event_count": int(len(events)),
                "rolling_60_median": float(eligible_rolling.median()),
                "rolling_60_p10": float(eligible_rolling.quantile(0.10)),
                "rolling_60_min": float(eligible_rolling.min()),
                "density_eligible": bool(
                    12 <= float(eligible_rolling.median()) <= 20
                    and float(eligible_rolling.quantile(0.10)) >= 8
                ),
            }
        ]
    )
    feature_evidence = pd.DataFrame(
        {
            "dt": calendar,
            "relative_strength_5": strength,
            "relative_amount_impulse": amount,
            "strength_z": strength_z,
            "amount_z": amount_z,
            "composite_score": composite,
            "prior_threshold": threshold,
            "active": active,
        }
    )
    return events, density, feature_evidence
