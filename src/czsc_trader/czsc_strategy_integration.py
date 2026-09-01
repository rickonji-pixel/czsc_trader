"""Pure helpers for integrating an admitted CZSC event into the four-layer strategy."""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd


def integrate_event_factor(
    champion_factors: pd.DataFrame,
    champion_weights: pd.Series,
    event: pd.Series,
    *,
    event_weight: float,
) -> pd.Series:
    """Add one positive event factor while preserving champion relative weights."""
    weight = float(event_weight)
    if not 0.0 < weight < 1.0:
        raise ValueError("event_weight must be strictly between zero and one")
    if list(champion_factors.columns) != list(champion_weights.index):
        raise ValueError("champion factor and weight identities differ")
    if not champion_factors.index.equals(event.index):
        raise ValueError("event index differs from champion factor index")
    values = event.astype(float)
    if not values.isin([0.0, 1.0]).all():
        raise ValueError("event factor must be binary")
    scaled = champion_weights.astype(float) * (1.0 - weight)
    score = champion_factors.astype(float).fillna(0.0).to_numpy() @ scaled.to_numpy()
    score = score + values.to_numpy() * weight
    return pd.Series(score, index=event.index, name="factor_score", dtype=float)


def strategy_candidate_passes(
    champion: Mapping[str, object],
    challenger: Mapping[str, object],
    *,
    return_tolerance: float,
    drawdown_tolerance: float,
) -> bool:
    """Apply the frozen return floor and strict maximum-drawdown improvement gate."""
    champion_return = float(champion["strategy_return"])
    challenger_return = float(challenger["strategy_return"])
    champion_drawdown = float(champion["max_drawdown"])
    challenger_drawdown = float(challenger["max_drawdown"])
    values = np.array(
        [champion_return, challenger_return, champion_drawdown, challenger_drawdown],
        dtype=float,
    )
    if not np.isfinite(values).all():
        return False
    return (
        challenger_return >= champion_return - float(return_tolerance)
        and challenger_drawdown > champion_drawdown + float(drawdown_tolerance)
    )
