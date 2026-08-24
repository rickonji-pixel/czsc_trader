from __future__ import annotations

import numpy as np
import pandas as pd

from czsc_trader.four_layer import (
    flatten_champion_weights,
    normalized_signal_factors,
    positions_from_scores,
    score_four_layer,
    validate_fixed_factor_weights,
)
from czsc_trader.factors import aggregate_signal_groups, signal_groups
from czsc_trader.rules import Rule, positions_for_rule


def _raw_columns() -> list[str]:
    return [
        "raw__30m__cxt_bi",
        "raw__30m__cxt_third_buy",
        "raw__daily__cxt_bi",
        "raw__daily__cxt_five_bi",
        "raw__daily__cxt_seven_bi",
        "raw__weekly__cxt_bi",
        "raw__daily__tas_ma_5",
        "raw__daily__tas_ma_10",
        "raw__daily__tas_ma_20",
        "raw__daily__tas_macd_cfg",
        "raw__daily__vol_window_cfg",
        "raw__daily__pressure_support_cfg",
    ]


def test_flattened_champion_keeps_all_twelve_cross_frequency_signals() -> None:
    columns = _raw_columns()
    groups = signal_groups(columns)

    weights = flatten_champion_weights(columns, groups, (0.3, 0.3, 0.4))

    assert weights.index.tolist() == columns
    assert len(weights) == 12
    np.testing.assert_allclose(weights.iloc[:6], 0.05)
    np.testing.assert_allclose(weights.iloc[6:10], 0.075)
    np.testing.assert_allclose(weights.iloc[10:], 0.20)
    assert any("30m" in name for name in weights.index)
    assert any("daily" in name for name in weights.index)
    assert any("weekly" in name for name in weights.index)


def test_flattened_score_and_positions_are_exactly_champion_equivalent() -> None:
    index = pd.bdate_range("2025-01-02", periods=6)
    raw = pd.DataFrame(
        {
            name: ["多头_任意_任意_0", "空头_任意_任意_0"] * 3
            for name in _raw_columns()
        },
        index=index,
    )
    factors = normalized_signal_factors(raw)
    groups = signal_groups(raw.columns)
    grouped = aggregate_signal_groups(factors, groups)
    champion = Rule((0.3, 0.3, 0.4), 0.15, 0.0, 1, 3, 1, "none")
    flat_weights = flatten_champion_weights(raw.columns, groups, champion.weights)

    flat_scores = score_four_layer(factors, flat_weights)
    flat_target = positions_from_scores(flat_scores, champion.enter, champion.exit, champion)
    champion_target, champion_scores = positions_for_rule(grouped, champion)

    np.testing.assert_allclose(flat_scores, champion_scores, rtol=0.0, atol=1e-12)
    pd.testing.assert_series_equal(flat_target, champion_target)


def test_weight_validation_rejects_dropped_or_unexpected_factors() -> None:
    expected = _raw_columns()
    weights = pd.Series(1.0 / 12.0, index=expected)

    validate_fixed_factor_weights(weights, expected, minimum_absolute_weight=0.005)

    with np.testing.assert_raises_regex(ValueError, "identities"):
        validate_fixed_factor_weights(weights.iloc[:-1], expected, 0.005)
    weights.iloc[0] = 0.0
    with np.testing.assert_raises_regex(ValueError, "nonzero"):
        validate_fixed_factor_weights(weights, expected, 0.005)


def test_linear_score_preserves_champion_groupwise_float_order_at_threshold() -> None:
    columns = _raw_columns()
    groups = signal_groups(columns)
    factors = pd.DataFrame(
        [[0, 0, 0, 0, 0, 0, 1, 1, 1, -1, 0, 0]],
        columns=columns,
        index=[pd.Timestamp("2021-01-06")],
        dtype=float,
    )
    weights = flatten_champion_weights(columns, groups, (0.3, 0.3, 0.4))

    score = score_four_layer(factors, weights)

    assert score.iloc[0] == 0.15
