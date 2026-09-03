"""Reusable closed-trade diagnostics for range-regime research."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .strategy_metrics import closed_trade_ledger


def range_cycle_objectives(
    orders: pd.DataFrame,
    regimes: pd.Series,
    sessions: pd.DatetimeIndex,
) -> tuple[tuple[str, float], ...]:
    """Summarize closed trades whose entry and exit signals are both in range."""
    cycles = closed_trade_ledger(orders).copy()
    if cycles.empty:
        return (("range_return", 0.0), ("range_trade_count", 0.0), ("range_short_loss_count", 0.0), ("range_to_trend_return", 0.0))
    labels = regimes.astype("string").copy()
    labels.index = pd.DatetimeIndex(pd.to_datetime(labels.index)).normalize()
    locations = pd.Series(np.arange(len(sessions)), index=pd.DatetimeIndex(sessions).normalize())
    for column in ("entry_signal_date", "exit_signal_date", "entry_date", "exit_date"):
        cycles[column] = pd.to_datetime(cycles[column])
    cycles["entry_regime"] = cycles["entry_signal_date"].dt.normalize().map(labels)
    cycles["exit_regime"] = cycles["exit_signal_date"].dt.normalize().map(labels)
    cycles["holding_sessions"] = (
        cycles["exit_date"].dt.normalize().map(locations)
        - cycles["entry_date"].dt.normalize().map(locations)
    )

    def compound(mask: pd.Series) -> float:
        values = cycles.loc[mask, "net_return"].astype(float)
        return float(np.prod(1.0 + values) - 1.0) if len(values) else 0.0

    pure = cycles["entry_regime"].eq("range") & cycles["exit_regime"].eq("range")
    transition = cycles["entry_regime"].eq("range") & cycles["exit_regime"].eq("trend")
    short_loss = pure & cycles["net_return"].astype(float).lt(0.0) & cycles["holding_sessions"].le(10)
    return (
        ("range_return", compound(pure)),
        ("range_trade_count", float(pure.sum())),
        ("range_short_loss_count", float(short_loss.sum())),
        ("range_to_trend_return", compound(transition)),
    )
