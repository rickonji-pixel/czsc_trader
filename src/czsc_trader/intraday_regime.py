"""Causal daily regime context for intraday strategy research."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .regime_weight import lagged_efficiency_ratio


REGIME_LABELS = ("trend_up", "range", "trend_down", "warmup")


def fit_lagged_er_threshold(
    close: pd.Series,
    *,
    lookback: int,
    calibration_start: pd.Timestamp | str,
    calibration_end: pd.Timestamp | str,
) -> float:
    """Fit one unsupervised threshold using only the declared calibration span."""

    er = lagged_efficiency_ratio(close.astype(float), int(lookback))
    sample = er.loc[
        pd.Timestamp(calibration_start) : pd.Timestamp(calibration_end)
    ].dropna()
    if sample.empty:
        raise ValueError("regime calibration has no valid observations")
    threshold = float(sample.median())
    if not np.isfinite(threshold):
        raise ValueError("regime threshold must be finite")
    return threshold


def classify_lagged_daily_regime(
    close: pd.Series,
    *,
    lookback: int,
    er_threshold: float,
) -> pd.DataFrame:
    """Classify each session from information available before that session opens."""

    if int(lookback) < 2:
        raise ValueError("regime lookback must be at least two")
    if not np.isfinite(float(er_threshold)):
        raise ValueError("regime threshold must be finite")
    values = close.astype(float)
    if not values.index.is_monotonic_increasing or values.index.has_duplicates:
        raise ValueError("daily close index must be unique and increasing")
    known = values.shift(1)
    direction_return = known.div(known.shift(int(lookback))).sub(1.0)
    efficiency = lagged_efficiency_ratio(values, int(lookback))
    labels = pd.Series("warmup", index=values.index, dtype="string", name="regime")
    ready = efficiency.notna() & direction_return.notna()
    trend = ready & efficiency.ge(float(er_threshold))
    labels.loc[ready & ~trend] = "range"
    labels.loc[trend & direction_return.ge(0.0)] = "trend_up"
    labels.loc[trend & direction_return.lt(0.0)] = "trend_down"
    return pd.DataFrame(
        {
            "known_close": known,
            "efficiency_ratio": efficiency,
            "direction_return": direction_return,
            "regime": labels,
        },
        index=values.index,
    )
