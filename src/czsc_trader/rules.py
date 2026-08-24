"""Deterministic fixed-rule scoring, positions, and factor events."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


FACTOR_COLUMNS = ("structure", "trend", "volume_position")


@dataclass(frozen=True)
class Rule:
    """A transparent score threshold and position-state rule."""

    weights: tuple[float, float, float]
    enter: float
    exit: float
    confirm_days: int
    min_hold_days: int
    exit_confirm_days: int = 1
    entry_gate: str = "none"


@dataclass(frozen=True)
class AppliedRule:
    """Signals produced by applying one already-selected fixed rule."""

    target_position: pd.Series
    scores: pd.Series
    events: pd.DataFrame


def positions_for_rule(factors: pd.DataFrame, rule: Rule) -> tuple[pd.Series, pd.Series]:
    """Apply a rule as a deterministic state machine without using prices."""
    clean = factors.loc[:, FACTOR_COLUMNS].fillna(0.0).astype(float)
    values = clean.to_numpy(dtype=float)
    scores_array = values @ np.asarray(rule.weights, dtype=float)
    gate_masks = {
        "none": np.ones(len(clean), dtype=bool),
        "structure": values[:, 0] >= 0.0,
        "trend": values[:, 1] >= 0.0,
        "structure_and_trend": (values[:, 0] >= 0.0) & (values[:, 1] >= 0.0),
    }
    if rule.entry_gate not in gate_masks:
        raise ValueError(f"unknown entry gate: {rule.entry_gate}")
    gate_mask = gate_masks[rule.entry_gate]
    positions: list[float] = []
    position = 0.0
    confirmations = 0
    exit_confirmations = 0
    holding_days = 0
    for score, gate_passes in zip(scores_array, gate_mask, strict=True):
        if position == 0.0:
            confirmations = confirmations + 1 if score >= rule.enter and gate_passes else 0
            if confirmations >= rule.confirm_days:
                position = 1.0
                holding_days = 1
                confirmations = 0
                exit_confirmations = 0
        else:
            eligible_exit = holding_days >= rule.min_hold_days
            exit_confirmations = (
                exit_confirmations + 1
                if eligible_exit and score <= rule.exit
                else 0
            )
            if exit_confirmations >= rule.exit_confirm_days:
                position = 0.0
                holding_days = 0
                confirmations = 0
                exit_confirmations = 0
            else:
                holding_days += 1
        positions.append(position)
    return (
        pd.Series(positions, index=factors.index, name="target_position", dtype=float),
        pd.Series(scores_array, index=factors.index, name="factor_score", dtype=float),
    )


def build_factor_events(
    target_position: pd.Series,
    scores: pd.Series,
    factors: pd.DataFrame,
    rule: Rule,
) -> pd.DataFrame:
    """Describe every position transition using only its CZSC factor snapshot."""
    target = target_position.astype(float)
    previous = target.shift(1, fill_value=0.0)
    rows: list[dict[str, object]] = []
    for signal_date in target.index[target.ne(previous)]:
        after = float(target.loc[signal_date])
        event_type = "Entry" if after == 1.0 else "Exit"
        score = float(scores.loc[signal_date])
        rows.append(
            {
                "event_id": f"Factor:{pd.Timestamp(signal_date):%Y%m%d}:{event_type}",
                "signal_date": pd.Timestamp(signal_date),
                "event_type": event_type,
                "structure": float(factors.loc[signal_date, "structure"]),
                "trend": float(factors.loc[signal_date, "trend"]),
                "volume_position": float(factors.loc[signal_date, "volume_position"]),
                "factor_score": score,
                "enter_threshold": float(rule.enter),
                "exit_threshold": float(rule.exit),
                "before_position": float(previous.loc[signal_date]),
                "after_position": after,
                "reason": (
                    f"score {score:.6f} >= enter {rule.enter:.6f}"
                    if event_type == "Entry"
                    else f"score {score:.6f} <= exit {rule.exit:.6f}"
                ),
            }
        )
    return pd.DataFrame(rows)


def apply_fixed_rule(factors: pd.DataFrame, rule: Rule) -> AppliedRule:
    """Apply one fixed rule without reading prices or candidate definitions."""
    aligned = factors.loc[:, FACTOR_COLUMNS].fillna(0.0).astype(float)
    target, scores = positions_for_rule(aligned, rule)
    events = build_factor_events(target, scores, aligned, rule)
    return AppliedRule(target, scores, events)
