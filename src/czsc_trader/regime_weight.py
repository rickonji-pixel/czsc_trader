"""Pure primitives for causal regime-conditioned factor weights."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

from .four_layer import score_four_layer


def lagged_efficiency_ratio(close: pd.Series, lookback: int = 60) -> pd.Series:
    """Return path efficiency known before each session opens."""
    if int(lookback) < 2:
        raise ValueError("ER lookback must be at least two")
    known = close.astype(float).shift(1)
    net = known.sub(known.shift(int(lookback))).abs()
    path = known.diff().abs().rolling(int(lookback), min_periods=int(lookback)).sum()
    return net.div(path.where(path.gt(0.0))).rename("er60")


def fit_regime_threshold(
    er: pd.Series, start: pd.Timestamp, end: pd.Timestamp
) -> float:
    """Fit one unsupervised median threshold inside the declared research span."""
    sample = er.loc[pd.Timestamp(start) : pd.Timestamp(end)].dropna().astype(float)
    if sample.empty:
        raise ValueError("regime calibration has no valid ER observations")
    value = float(sample.median())
    if not np.isfinite(value):
        raise ValueError("regime threshold must be finite")
    return value


def classify_regimes(er: pd.Series, threshold: float) -> pd.Series:
    """Label finite observations as trend/range and missing observations as warmup."""
    if not np.isfinite(float(threshold)):
        raise ValueError("regime threshold must be finite")
    labels = pd.Series("warmup", index=er.index, dtype="string", name="regime")
    tied = pd.Series(
        np.isclose(er.astype(float), float(threshold), rtol=0.0, atol=1e-15),
        index=er.index,
    )
    trend = er.notna() & (er.ge(float(threshold)) | tied)
    labels.loc[er.notna() & ~trend] = "range"
    labels.loc[trend] = "trend"
    return labels


def project_group_weights(
    base_weights: pd.Series,
    groups: Mapping[str, Sequence[str]],
    trend_multiplier: float,
    volume_multiplier: float,
) -> pd.Series:
    """Apply two group multipliers while preserving identities, signs, and L1 scale."""
    required = ("structure", "trend", "volume_position")
    if any(name not in groups for name in required):
        raise ValueError("factor groups must contain structure, trend, and volume_position")
    membership = [str(item) for name in required for item in groups[name]]
    if len(membership) != len(set(membership)) or set(membership) != set(base_weights.index):
        raise ValueError("factor group membership differs from baseline weights")
    multipliers = {
        "structure": 1.0,
        "trend": float(trend_multiplier),
        "volume_position": float(volume_multiplier),
    }
    if any(not np.isfinite(value) or value <= 0.0 for value in multipliers.values()):
        raise ValueError("group multipliers must be positive finite values")
    projected = base_weights.astype(float).copy()
    for name in required:
        projected.loc[list(groups[name])] *= multipliers[name]
    scale = float(projected.abs().sum())
    if not np.isfinite(scale) or scale <= 0.0:
        raise ValueError("projected weights have invalid L1 scale")
    projected /= scale
    projected.name = "weight"
    return projected


def score_with_regime_weights(
    factors: pd.DataFrame,
    regimes: pd.Series,
    weights: Mapping[str, pd.Series],
    fallback: pd.Series,
) -> pd.Series:
    """Score each row with the weight vector selected by its causal regime label."""
    if not factors.index.equals(regimes.index):
        raise ValueError("factor and regime indices differ")
    if set(regimes.astype(str).unique()) - {"trend", "range", "warmup"}:
        raise ValueError("unknown regime label")
    if set(weights) != {"trend", "range"}:
        raise ValueError("regime weights must contain trend and range")
    output = pd.Series(index=factors.index, dtype=float, name="factor_score")
    choices = {"trend": weights["trend"], "range": weights["range"], "warmup": fallback}
    for label, selected in choices.items():
        mask = regimes.astype(str).eq(label)
        if mask.any():
            output.loc[mask] = score_four_layer(factors.loc[mask], selected)
    if output.isna().any():
        raise AssertionError("regime scoring left missing values")
    return output
