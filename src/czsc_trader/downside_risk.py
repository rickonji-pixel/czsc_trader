"""Causal downside-risk states and position multipliers."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def _validate_index(values: pd.Series, name: str) -> None:
    if not values.index.is_monotonic_increasing or values.index.has_duplicates:
        raise ValueError(f"{name} index must be unique and increasing")


@dataclass(frozen=True)
class DownsideRiskSpec:
    lookback: int
    stress_quantile: float
    pressure_position: float

    def __post_init__(self) -> None:
        if int(self.lookback) != self.lookback or self.lookback <= 1:
            raise ValueError("lookback must be an integer greater than 1")
        if not np.isfinite(self.stress_quantile) or not 0.0 < self.stress_quantile < 1.0:
            raise ValueError("stress_quantile must be strictly between 0 and 1")
        if not np.isfinite(self.pressure_position) or not 0.0 < self.pressure_position < 1.0:
            raise ValueError("pressure_position must be strictly between 0 and 1")

    @property
    def candidate_id(self) -> str:
        return (
            f"dv_L{int(self.lookback)}_Q{round(self.stress_quantile * 100):02d}"
            f"_P{self.pressure_position:.2f}"
        )


def downside_volatility(close: pd.Series, lookback: int) -> pd.Series:
    """Annualized rolling semideviation of completed negative log returns."""
    if int(lookback) != lookback or lookback <= 1:
        raise ValueError("lookback must be an integer greater than 1")
    values = close.astype(float)
    _validate_index(values, "close")
    if values.isna().any() or not np.isfinite(values).all() or not values.gt(0.0).all():
        raise ValueError("close prices must be positive and finite")
    negative = np.log(values / values.shift(1)).clip(upper=0.0)
    result = np.sqrt(
        252.0 * negative.pow(2).rolling(int(lookback), min_periods=int(lookback)).mean()
    )
    return result.rename("downside_volatility")


def downside_stress_threshold(
    downside_vol: pd.Series,
    quantile: float,
    history: int = 252,
) -> pd.Series:
    """Lagged rolling quantile that excludes the current risk observation."""
    if not np.isfinite(quantile) or not 0.0 < quantile < 1.0:
        raise ValueError("quantile must be strictly between 0 and 1")
    if int(history) != history or history <= 1:
        raise ValueError("history must be an integer greater than 1")
    values = downside_vol.astype(float)
    _validate_index(values, "downside volatility")
    finite = values.dropna()
    if not np.isfinite(finite).all() or finite.lt(0.0).any():
        raise ValueError("downside volatility must contain only nonnegative finite values")
    return (
        values.shift(1)
        .rolling(int(history), min_periods=int(history))
        .quantile(float(quantile))
        .rename("stress_threshold")
    )


def downside_stress(
    downside_vol: pd.Series,
    quantile: float,
    history: int = 252,
) -> pd.Series:
    """Return a causal stress flag using the prior-history threshold."""
    threshold = downside_stress_threshold(downside_vol, quantile, history)
    return downside_vol.gt(threshold).fillna(False).rename("downside_stress")


def downside_risk_target(
    baseline_target: pd.Series,
    stress: pd.Series,
    pressure_position: float,
) -> pd.Series:
    """Compose the champion gate with a full/pressure risk multiplier."""
    if not baseline_target.index.equals(stress.index):
        raise ValueError("baseline target and stress indices must match")
    if not np.isfinite(pressure_position) or not 0.0 < pressure_position < 1.0:
        raise ValueError("pressure_position must be strictly between 0 and 1")
    baseline = baseline_target.astype(float)
    _validate_index(baseline, "baseline target")
    if baseline.isna().any() or not baseline.isin([0.0, 1.0]).all():
        raise ValueError("baseline target must contain only 0 or 1")
    if stress.isna().any():
        raise ValueError("stress state must not contain missing values")
    multiplier = pd.Series(
        np.where(stress.astype(bool), float(pressure_position), 1.0),
        index=baseline.index,
    )
    return (baseline * multiplier).rename("target_position")


DOWNSIDE_EVENT_COLUMNS = (
    "event_id",
    "signal_date",
    "event_type",
    "factor_score",
    "downside_volatility",
    "stress_threshold",
    "stress",
    "lookback",
    "stress_quantile",
    "pressure_position",
    "candidate_id",
    "baseline_position",
    "before_position",
    "after_position",
    "reason",
)


def build_downside_risk_events(
    target_position: pd.Series,
    baseline_target: pd.Series,
    scores: pd.Series,
    downside_vol: pd.Series,
    stress_threshold: pd.Series,
    stress: pd.Series,
    spec: DownsideRiskSpec,
) -> pd.DataFrame:
    """Describe every champion or downside-risk position transition."""
    inputs = (baseline_target, scores, downside_vol, stress_threshold, stress)
    if any(not target_position.index.equals(values.index) for values in inputs):
        raise ValueError("downside-risk event inputs must have identical indices")
    target = target_position.astype(float)
    if target.isna().any() or not target.isin(
        [0.0, float(spec.pressure_position), 1.0]
    ).all():
        raise ValueError("target contains an unsupported downside-risk position")
    previous = target.shift(1, fill_value=0.0)
    rows: list[dict[str, object]] = []
    for signal_date in target.index[target.ne(previous)]:
        before = float(previous.loc[signal_date])
        after = float(target.loc[signal_date])
        if before == 0.0 and after > 0.0:
            event_type = "Entry"
            reason = "champion entry with current downside-risk multiplier"
        elif 0.0 < before < after:
            event_type = "Increase"
            reason = "downside-risk stress cleared while champion remained active"
        elif before > after > 0.0:
            event_type = "Reduce"
            reason = "downside-risk stress activated while champion remained active"
        elif before > 0.0 and after == 0.0:
            event_type = "Exit"
            reason = "champion exit"
        else:
            raise ValueError(f"unsupported downside-risk transition {before} -> {after}")
        threshold_value = stress_threshold.loc[signal_date]
        rows.append(
            {
                "event_id": (
                    f"DownsideRisk:{spec.candidate_id}:"
                    f"{pd.Timestamp(signal_date):%Y%m%d}:{event_type}"
                ),
                "signal_date": pd.Timestamp(signal_date),
                "event_type": event_type,
                "factor_score": float(scores.loc[signal_date]),
                "downside_volatility": float(downside_vol.loc[signal_date]),
                "stress_threshold": (
                    float(threshold_value) if pd.notna(threshold_value) else np.nan
                ),
                "stress": bool(stress.loc[signal_date]),
                "lookback": int(spec.lookback),
                "stress_quantile": float(spec.stress_quantile),
                "pressure_position": float(spec.pressure_position),
                "candidate_id": spec.candidate_id,
                "baseline_position": float(baseline_target.loc[signal_date]),
                "before_position": before,
                "after_position": after,
                "reason": reason,
            }
        )
    return pd.DataFrame(rows, columns=DOWNSIDE_EVENT_COLUMNS)
