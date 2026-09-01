"""Causal moving-average strategy signals."""

from __future__ import annotations

import numpy as np
import pandas as pd


def moving_average_signals(
    daily: pd.DataFrame,
    fast: int = 5,
    slow: int = 20,
) -> pd.DataFrame:
    """Return close-known MA signals; execution is delegated to the next-open engine."""
    fast = int(fast)
    slow = int(slow)
    if fast < 1 or slow < 2 or fast >= slow:
        raise ValueError("moving-average windows must satisfy 1 <= fast < slow")
    if "dt" not in daily or "close" not in daily:
        raise ValueError("daily prices require dt and close columns")
    frame = daily.loc[:, ["dt", "close"]].copy()
    frame["dt"] = pd.to_datetime(frame["dt"])
    frame = frame.set_index("dt").sort_index()
    frame.index = pd.DatetimeIndex(frame.index, name="dt")
    if frame.index.has_duplicates:
        raise ValueError("daily prices contain duplicate dates")
    frame["close"] = frame["close"].astype(float)
    if frame.empty or not np.isfinite(frame["close"].to_numpy()).all():
        raise ValueError("daily close prices must be finite and non-empty")
    frame[f"ma{fast}"] = frame["close"].rolling(fast, min_periods=fast).mean()
    frame[f"ma{slow}"] = frame["close"].rolling(slow, min_periods=slow).mean()
    frame["target_position"] = (
        frame[f"ma{fast}"].gt(frame[f"ma{slow}"])
    ).astype(float)
    return frame
