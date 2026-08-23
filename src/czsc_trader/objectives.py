"""Single source of truth for research periods and absolute return targets."""

from __future__ import annotations

import pandas as pd


TARGET_PERIODS = {
    "2026Q1": (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-03-31")),
    "2026H1": (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-06-30")),
    "2026M1-M8": (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-08-21")),
}

RETURN_TARGETS = {
    "2026Q1": 0.105,
    "2026H1": 0.825,
    "2026M1-M8": 0.600,
}


def evaluate_return(
    name: str,
    strategy_return: float,
    targets: dict[str, float] | None = None,
) -> dict[str, float | bool]:
    """Evaluate one strategy return against its inclusive absolute target."""
    target = float((RETURN_TARGETS if targets is None else targets)[name])
    margin = float(strategy_return) - target
    return {
        "target_return": target,
        "target_margin": margin,
        "pass": margin >= 0.0,
    }


def overall_pass(windows: dict[str, dict[str, object]]) -> bool:
    """Return true only when every declared window reaches its target."""
    if set(windows) != set(RETURN_TARGETS):
        return False
    return all(
        bool(evaluate_return(name, float(windows[name]["strategy_return"]))["pass"])
        for name in RETURN_TARGETS
    )
