"""Exact fixed-factor four-layer representation of the frozen champion."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

from .factors import map_signal_frame, signal_groups
from .rules import Rule


def normalized_signal_factors(raw: pd.DataFrame) -> pd.DataFrame:
    """Treat the champion's frozen semantic normalization as factor definition."""
    mapped, _ = map_signal_frame(raw)
    return mapped


def flatten_champion_weights(
    columns: Sequence[str],
    groups: Mapping[str, Sequence[str]],
    group_weights: Sequence[float],
) -> pd.Series:
    """Algebraically flatten group means into one weight per fixed signal."""
    group_names = ("structure", "trend", "volume_position")
    if len(group_weights) != len(group_names):
        raise ValueError("champion must provide three group weights")
    membership: dict[str, str] = {}
    for group_name in group_names:
        members = tuple(groups[group_name])
        if not members:
            raise ValueError(f"champion group is empty: {group_name}")
        for member in members:
            if member in membership:
                raise ValueError(f"signal belongs to multiple groups: {member}")
            membership[member] = group_name
    if set(membership) != set(columns):
        raise ValueError("group membership differs from fixed signal identities")
    group_weight_map = dict(zip(group_names, map(float, group_weights), strict=True))
    values = {
        column: group_weight_map[membership[column]] / len(groups[membership[column]])
        for column in columns
    }
    return pd.Series(values, index=list(columns), name="weight", dtype=float)


def validate_fixed_factor_weights(
    weights: pd.Series,
    expected_columns: Sequence[str],
    minimum_absolute_weight: float,
) -> None:
    """Reject factor addition, removal, reordering, zeroing, or invalid normalization."""
    if list(weights.index) != list(expected_columns):
        raise ValueError("weight identities or order differ from fixed factors")
    values = weights.astype(float)
    if not np.isfinite(values).all():
        raise ValueError("weights must be finite")
    if values.abs().lt(float(minimum_absolute_weight) - 1e-15).any():
        raise ValueError("every fixed factor must retain a nonzero weight")
    if abs(float(values.abs().sum()) - 1.0) > 1e-12:
        raise ValueError("fixed-factor weights must have L1 norm one")


def score_four_layer(factors: pd.DataFrame, weights: pd.Series) -> pd.Series:
    """Apply the fixed linear scoring formula without another mapping layer."""
    if list(factors.columns) != list(weights.index):
        raise ValueError("factor columns differ from weight identities")
    clean = factors.fillna(0.0).astype(float)
    groups = signal_groups(clean.columns)
    ordered_groups = ("structure", "trend", "volume_position")
    grouped_names = [name for group in ordered_groups for name in groups[group]]
    if set(grouped_names) == set(clean.columns) and all(groups[group] for group in ordered_groups):
        # This remains one linear sum over 12 factor-weight products.  Summing
        # the three frozen membership slices separately preserves the
        # champion's floating-point threshold behavior at exact boundaries.
        subtotals = np.column_stack(
            [
                (
                    clean.loc[:, list(groups[group])].mean(axis=1).to_numpy()
                    * float(weights.loc[list(groups[group])].sum())
                    + clean.loc[:, list(groups[group])].to_numpy()
                    @ (
                        weights.loc[list(groups[group])].astype(float).to_numpy()
                        - float(weights.loc[list(groups[group])].mean())
                    )
                )
                for group in ordered_groups
            ]
        )
        scores = subtotals @ np.ones(len(ordered_groups), dtype=float)
    else:
        scores = clean.to_numpy() @ weights.astype(float).to_numpy()
    return pd.Series(scores, index=factors.index, name="factor_score", dtype=float)


def positions_from_scores(
    scores: pd.Series,
    enter: float,
    exit: float,
    state_rule: Rule,
) -> pd.Series:
    """Apply only the frozen champion state machine to four-layer scores."""
    if float(exit) >= float(enter):
        raise ValueError("exit threshold must be below entry threshold")
    if state_rule.entry_gate != "none":
        raise ValueError("fixed-factor challenger requires champion entry_gate none")
    position = 0.0
    confirmations = 0
    exit_confirmations = 0
    holding_days = 0
    output: list[float] = []
    for score in scores.astype(float):
        if position == 0.0:
            confirmations = confirmations + 1 if score >= enter else 0
            if confirmations >= state_rule.confirm_days:
                position = 1.0
                holding_days = 1
                confirmations = 0
                exit_confirmations = 0
        else:
            eligible = holding_days >= state_rule.min_hold_days
            exit_confirmations = exit_confirmations + 1 if eligible and score <= exit else 0
            if exit_confirmations >= state_rule.exit_confirm_days:
                position = 0.0
                holding_days = 0
                confirmations = 0
                exit_confirmations = 0
            else:
                holding_days += 1
        output.append(position)
    return pd.Series(output, index=scores.index, name="target_position", dtype=float)
