"""Apply either supported immutable baseline through one audit-compatible API."""

from __future__ import annotations

import pandas as pd

from .baselines import ResolvedBaseline
from .four_layer import (
    build_four_layer_events,
    normalized_signal_factors,
    positions_from_scores,
    score_four_layer,
    validate_fixed_factor_weights,
)
from .rules import AppliedRule, apply_fixed_rule


def apply_resolved_baseline(
    factor_frame: pd.DataFrame,
    baseline: ResolvedBaseline,
) -> AppliedRule:
    """Execute one already-frozen baseline without candidate generation."""
    if baseline.strategy == "czsc_fixed_rule":
        return apply_fixed_rule(factor_frame, baseline.rule)
    if baseline.strategy != "czsc_four_layer":
        raise ValueError(f"unknown baseline strategy: {baseline.strategy}")
    names = tuple(map(str, baseline.factor_names))
    if len(names) != len(set(names)):
        raise ValueError("four-layer frozen factor identities must be unique")
    missing = sorted(set(names) - set(map(str, factor_frame.columns)))
    if missing:
        raise ValueError(f"missing frozen factors: {missing}")
    raw = factor_frame.loc[:, list(names)]
    factors = normalized_signal_factors(raw)
    weights = pd.Series(baseline.factor_weights, index=names, name="weight", dtype=float)
    validate_fixed_factor_weights(weights, names, minimum_absolute_weight=0.005)
    scores = score_four_layer(factors, weights)
    target = positions_from_scores(
        scores,
        baseline.rule.enter,
        baseline.rule.exit,
        baseline.rule,
    )
    events = build_four_layer_events(
        target,
        scores,
        baseline.rule.enter,
        baseline.rule.exit,
    )
    return AppliedRule(target, scores, events)
