"""Causal fixed score tiers and fractional-position state machine."""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from .rules import Rule


TIER_MAPPINGS: dict[str, dict[int, float]] = {
    "M1": {0: 0.0, 1: 0.5, 2: 0.75, 3: 1.0, 4: 1.0},
    "M2": {0: 0.0, 1: 0.5, 2: 0.5, 3: 0.75, 4: 1.0},
    "M3": {0: 0.0, 1: 0.5, 2: 0.75, 3: 0.75, 4: 1.0},
    "M4": {0: 0.0, 1: 0.5, 2: 0.5, 3: 1.0, 4: 1.0},
}


def classify_score_tiers(scores: pd.Series) -> pd.Series:
    """Classify scores using the five preregistered absolute intervals."""
    values = scores.astype(float)
    if values.isna().any() or not np.isfinite(values).all():
        raise ValueError("scores must be finite")
    tiers = np.select(
        [
            values.le(0.025),
            values.lt(0.075),
            values.lt(0.125),
            values.lt(0.175),
        ],
        [0, 1, 2, 3],
        default=4,
    )
    return pd.Series(tiers, index=values.index, name="score_tier", dtype=int)


def build_tier_mapping(candidate_id: str) -> dict[int, float]:
    """Return a copy of one of the four immutable tier mappings."""
    if candidate_id not in TIER_MAPPINGS:
        raise ValueError(f"unknown score-tier mapping: {candidate_id}")
    return dict(TIER_MAPPINGS[candidate_id])


def positions_from_score_tiers(
    scores: pd.Series,
    rule: Rule,
    candidate_id: str,
    resize_confirm_days: int = 2,
) -> pd.Series:
    """Map completed daily scores to one causal fractional target path."""
    if rule.entry_gate != "none":
        raise ValueError("score-tier positions require entry_gate none")
    if resize_confirm_days < 1:
        raise ValueError("resize_confirm_days must be positive")
    values = scores.astype(float)
    if values.isna().any() or not np.isfinite(values).all():
        raise ValueError("scores must be finite")
    mapping = build_tier_mapping(candidate_id)
    tiers = classify_score_tiers(values)
    position = 0.0
    entry_confirmations = 0
    exit_confirmations = 0
    holding_days = 0
    pending_position: float | None = None
    pending_days = 0
    output: list[float] = []
    for score, tier in zip(values, tiers, strict=True):
        if position == 0.0:
            entry_confirmations = entry_confirmations + 1 if score >= rule.enter else 0
            if entry_confirmations >= rule.confirm_days:
                position = 1.0
                holding_days = 1
                entry_confirmations = 0
                exit_confirmations = 0
                pending_position = None
                pending_days = 0
        else:
            eligible = holding_days >= rule.min_hold_days
            exit_confirmations = (
                exit_confirmations + 1 if eligible and score <= rule.exit else 0
            )
            if exit_confirmations >= rule.exit_confirm_days:
                position = 0.0
                holding_days = 0
                entry_confirmations = 0
                exit_confirmations = 0
                pending_position = None
                pending_days = 0
            else:
                if eligible and score > rule.exit:
                    desired = float(mapping[int(tier)])
                    if desired == position:
                        pending_position = None
                        pending_days = 0
                    elif desired == pending_position:
                        pending_days += 1
                    else:
                        pending_position = desired
                        pending_days = 1
                    if pending_days >= resize_confirm_days:
                        position = desired
                        pending_position = None
                        pending_days = 0
                else:
                    pending_position = None
                    pending_days = 0
                holding_days += 1
        output.append(position)
    target = pd.Series(output, index=values.index, name="target_position", dtype=float)
    if not target.isin([0.0, 0.5, 0.75, 1.0]).all():
        raise AssertionError("score-tier state machine emitted an invalid target")
    return target


def score_tier_candidate_passes(
    champion: Mapping[str, object],
    challenger: Mapping[str, object],
    *,
    return_tolerance: float,
    drawdown_tolerance: float,
) -> bool:
    """Apply the frozen EX13 return floor and strict drawdown gate."""
    champion_return = float(champion["strategy_return"])
    challenger_return = float(challenger["strategy_return"])
    champion_drawdown = float(champion["max_drawdown"])
    challenger_drawdown = float(challenger["max_drawdown"])
    values = np.array(
        [champion_return, challenger_return, champion_drawdown, challenger_drawdown]
    )
    return bool(
        np.isfinite(values).all()
        and challenger_return >= champion_return - float(return_tolerance)
        and challenger_drawdown > champion_drawdown + float(drawdown_tolerance)
    )
