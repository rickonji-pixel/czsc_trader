"""Pure counterfactual and attribution primitives for frozen-rule research."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from itertools import combinations
from math import factorial

import pandas as pd

from .factors import aggregate_signal_groups
from .rules import Rule


def all_coalitions(names: Sequence[str]) -> tuple[frozenset[str], ...]:
    """Return every coalition in deterministic size and input-order order."""
    ordered = tuple(names)
    return tuple(
        frozenset(items)
        for size in range(len(ordered) + 1)
        for items in combinations(ordered, size)
    )


def _validated_utilities(
    utilities: Mapping[frozenset[str], float], names: Sequence[str]
) -> tuple[str, ...]:
    ordered = tuple(names)
    expected = set(all_coalitions(ordered))
    if set(utilities) != expected:
        raise ValueError("utilities must contain every declared coalition exactly once")
    return ordered


def shapley_values(
    utilities: Mapping[frozenset[str], float], names: Sequence[str]
) -> dict[str, float]:
    """Compute exact Shapley values for a complete finite coalition game."""
    ordered = _validated_utilities(utilities, names)
    count = len(ordered)
    values: dict[str, float] = {}
    for name in ordered:
        others = tuple(item for item in ordered if item != name)
        contribution = 0.0
        for coalition in all_coalitions(others):
            size = len(coalition)
            weight = factorial(size) * factorial(count - size - 1) / factorial(count)
            contribution += weight * (
                float(utilities[coalition | {name}]) - float(utilities[coalition])
            )
        values[name] = contribution
    return values


def shapley_interactions(
    utilities: Mapping[frozenset[str], float], names: Sequence[str]
) -> dict[tuple[str, str], float]:
    """Compute exact pairwise Shapley interaction indices."""
    ordered = _validated_utilities(utilities, names)
    count = len(ordered)
    if count < 2:
        return {}
    interactions: dict[tuple[str, str], float] = {}
    for left_index, left in enumerate(ordered):
        for right in ordered[left_index + 1 :]:
            others = tuple(item for item in ordered if item not in {left, right})
            value = 0.0
            for coalition in all_coalitions(others):
                size = len(coalition)
                weight = (
                    factorial(size)
                    * factorial(count - size - 2)
                    / factorial(count - 1)
                )
                second_difference = (
                    float(utilities[coalition | {left, right}])
                    - float(utilities[coalition | {left}])
                    - float(utilities[coalition | {right}])
                    + float(utilities[coalition])
                )
                value += weight * second_difference
            interactions[(left, right)] = value
    return interactions


def classify_annual_effect(
    rows: pd.DataFrame,
    return_epsilon: float,
    sharpe_epsilon: float,
    stable_years: int,
) -> str:
    """Classify one removal counterfactual using preregistered annual rules."""
    required = {"return_delta", "sharpe_delta", "dominant_share"}
    if not required <= set(rows.columns):
        raise ValueError(f"annual rows missing columns: {sorted(required - set(rows.columns))}")
    returns = rows["return_delta"].astype(float)
    sharpes = rows["sharpe_delta"].astype(float)
    dominated = rows["dominant_share"].astype(float).gt(0.5).any()

    stable_negative = ((returns > return_epsilon) & (sharpes > sharpe_epsilon)).sum()
    stable_positive = ((returns < -return_epsilon) & (sharpes < -sharpe_epsilon)).sum()
    return_positive_risk_negative = (
        (returns < -return_epsilon) & (sharpes > sharpe_epsilon)
    ).sum()
    risk_positive_return_negative = (
        (returns > return_epsilon) & (sharpes < -sharpe_epsilon)
    ).sum()
    redundant = (
        returns.abs().le(return_epsilon) & sharpes.abs().le(sharpe_epsilon)
    ).sum()

    if stable_negative >= stable_years:
        return "inconclusive" if dominated else "stable_negative"
    if stable_positive >= stable_years:
        return "inconclusive" if dominated else "stable_positive"
    if return_positive_risk_negative >= stable_years:
        return "return_positive_risk_negative"
    if risk_positive_return_negative >= stable_years:
        return "risk_positive_return_negative"
    if redundant >= stable_years:
        return "redundant"
    if stable_negative and stable_positive:
        return "regime_dependent"
    return "inconclusive"


def dominant_difference_share(
    champion_target: pd.Series,
    counterfactual_target: pd.Series,
    daily_return_delta: pd.Series,
) -> float:
    """Return the largest contiguous disagreement contribution share."""
    index = champion_target.index
    counterfactual = counterfactual_target.reindex(index)
    returns = daily_return_delta.reindex(index).fillna(0.0).astype(float)
    if counterfactual.isna().any():
        raise ValueError("counterfactual target must cover the champion index")
    differs = champion_target.astype(float).ne(counterfactual.astype(float))
    if not differs.any():
        return 0.0
    group_ids = differs.ne(differs.shift(fill_value=False)).cumsum()
    contributions = returns.loc[differs].groupby(group_ids.loc[differs]).sum().abs()
    total = float(contributions.sum())
    return float(contributions.max() / total) if total > 0.0 else 0.0


def zero_signal_contribution(
    mapped: pd.DataFrame,
    column: str,
    mask: pd.Series | None = None,
) -> pd.DataFrame:
    """Return a copy with one mapped signal contribution neutralized."""
    if column not in mapped:
        raise KeyError(column)
    result = mapped.copy()
    selected = pd.Series(True, index=result.index) if mask is None else mask.reindex(result.index)
    if selected.isna().any():
        raise ValueError("state mask must cover the mapped signal index")
    result.loc[selected.astype(bool), column] = 0.0
    return result


def drop_signal_and_reaggregate(
    mapped: pd.DataFrame,
    groups: Mapping[str, Sequence[str]],
    column: str,
) -> pd.DataFrame:
    """Drop one signal and recompute the champion aggregate group means."""
    if column not in mapped:
        raise KeyError(column)
    remaining = mapped.drop(columns=[column])
    reduced_groups = {
        name: tuple(item for item in members if item != column)
        for name, members in groups.items()
    }
    return aggregate_signal_groups(remaining, reduced_groups)


def rule_with_weight_delta(rule: Rule, factor_index: int, delta: float) -> Rule:
    """Move one weight and compensate equally across the other two weights."""
    if factor_index not in range(len(rule.weights)):
        raise IndexError(factor_index)
    if len(rule.weights) != 3:
        raise ValueError("weight sensitivity requires exactly three factors")
    weights = list(rule.weights)
    weights[factor_index] += float(delta)
    compensation = float(delta) / 2.0
    for index in range(3):
        if index != factor_index:
            weights[index] -= compensation
    if any(weight < 0.0 for weight in weights):
        raise ValueError("weight sensitivity produced a negative weight")
    return replace(rule, weights=tuple(weights))
