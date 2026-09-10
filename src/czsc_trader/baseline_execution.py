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
from .regime_weight import classify_regimes, lagged_efficiency_ratio, score_with_regime_weights
from .rules import AppliedRule, apply_fixed_rule


def apply_resolved_baseline(
    factor_frame: pd.DataFrame,
    baseline: ResolvedBaseline,
    *,
    daily_close: pd.Series | None = None,
    normalized_factors: pd.DataFrame | None = None,
    regimes: pd.Series | None = None,
) -> AppliedRule:
    """Execute one already-frozen baseline without candidate generation."""
    if baseline.strategy == "czsc_fixed_rule":
        if baseline.rule is None:
            raise ValueError("fixed-rule baseline has no rule")
        return apply_fixed_rule(factor_frame, baseline.rule)
    if baseline.strategy not in {"czsc_four_layer", "czsc_regime_weight"}:
        raise ValueError(f"unknown baseline strategy: {baseline.strategy}")
    if baseline.rule is None:
        raise ValueError("factor baseline has no rule")
    names = tuple(map(str, baseline.factor_names))
    if len(names) != len(set(names)):
        raise ValueError("four-layer frozen factor identities must be unique")
    missing = sorted(set(names) - set(map(str, factor_frame.columns)))
    if missing:
        raise ValueError(f"missing frozen factors: {missing}")
    if normalized_factors is None:
        raw = factor_frame.loc[:, list(names)]
        factors = normalized_signal_factors(raw)
    else:
        factors = normalized_factors
        if list(factors.columns) != list(names) or not factors.index.equals(factor_frame.index):
            raise ValueError("prepared normalized factors differ from frozen factor identities")
    weights = pd.Series(baseline.factor_weights, index=names, name="weight", dtype=float)
    validate_fixed_factor_weights(weights, names, minimum_absolute_weight=0.005)
    applied_regimes: pd.Series | None = None
    if baseline.strategy == "czsc_four_layer":
        scores = score_four_layer(factors, weights)
    else:
        if daily_close is None:
            raise ValueError("regime-weight baseline requires causal daily close prices")
        close = daily_close.astype(float).copy()
        close.index = pd.DatetimeIndex(pd.to_datetime(close.index), name=factors.index.name)
        close = close.reindex(factors.index)
        if close.isna().any():
            raise ValueError("regime-weight daily close prices do not align to factors")
        if regimes is None:
            regimes = classify_regimes(
                lagged_efficiency_ratio(close, baseline.er_lookback),
                baseline.er_threshold,
            )
        elif not regimes.index.equals(factors.index):
            raise ValueError("prepared regimes do not align to factors")
        applied_regimes = regimes.astype("string").copy()
        regime_weights = {
            label: pd.Series(values, index=names, name="weight", dtype=float)
            for label, values in baseline.regime_factor_weights.items()
        }
        for selected in regime_weights.values():
            validate_fixed_factor_weights(selected, names, minimum_absolute_weight=0.005)
        scores = score_with_regime_weights(factors, regimes, regime_weights, weights)
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
    return AppliedRule(target, scores, events, applied_regimes)
