from __future__ import annotations

import pandas as pd
import pytest

from czsc_trader.attribution import (
    all_coalitions,
    classify_annual_effect,
    dominant_difference_share,
    drop_signal_and_reaggregate,
    rule_with_weight_delta,
    shapley_interactions,
    shapley_values,
    zero_signal_contribution,
)
from czsc_trader.rules import Rule


def test_exact_shapley_values_match_hand_calculated_additive_game() -> None:
    """Catch wrong factorial weights or nondeterministic coalition handling."""
    names = ("a", "b", "c")
    utilities = {
        coalition: sum({"a": 1.0, "b": 2.0, "c": 3.0}[name] for name in coalition)
        for coalition in all_coalitions(names)
    }

    values = shapley_values(utilities, names)

    assert values == {"a": 1.0, "b": 2.0, "c": 3.0}
    assert list(all_coalitions(names)) == [
        frozenset(),
        frozenset({"a"}),
        frozenset({"b"}),
        frozenset({"c"}),
        frozenset({"a", "b"}),
        frozenset({"a", "c"}),
        frozenset({"b", "c"}),
        frozenset({"a", "b", "c"}),
    ]


def test_pairwise_interaction_is_one_for_pure_ab_synergy() -> None:
    """Catch interaction formulas that omit or double-count coalitions."""
    names = ("a", "b", "c")
    utilities = {
        coalition: float({"a", "b"} <= coalition)
        for coalition in all_coalitions(names)
    }

    interactions = shapley_interactions(utilities, names)

    assert interactions[("a", "b")] == pytest.approx(1.0)
    assert interactions[("a", "c")] == pytest.approx(0.0)
    assert interactions[("b", "c")] == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("return_deltas", "sharpe_deltas", "expected"),
    [
        ([0.01] * 4 + [-0.01], [0.10] * 4 + [-0.10], "stable_negative"),
        ([-0.01] * 4 + [0.01], [-0.10] * 4 + [0.10], "stable_positive"),
        ([-0.01] * 4 + [0.00], [0.10] * 4 + [0.00], "return_positive_risk_negative"),
        ([0.01] * 4 + [0.00], [-0.10] * 4 + [0.00], "risk_positive_return_negative"),
        ([0.0] * 5, [0.0] * 5, "redundant"),
        ([0.01, -0.01, 0.01, -0.01, 0.0], [0.10, -0.10, 0.10, -0.10, 0.0], "regime_dependent"),
        ([0.01, 0.01, 0.0, 0.0, 0.0], [0.10, 0.10, 0.0, 0.0, 0.0], "inconclusive"),
    ],
)
def test_annual_classification_uses_preregistered_directions(
    return_deltas: list[float], sharpe_deltas: list[float], expected: str
) -> None:
    """Catch sign reversals between removal effects and factor classifications."""
    rows = pd.DataFrame(
        {
            "return_delta": return_deltas,
            "sharpe_delta": sharpe_deltas,
            "dominant_share": [0.2] * 5,
        }
    )

    assert classify_annual_effect(rows, 0.001, 0.02, 4) == expected


def test_dominance_over_half_disqualifies_stable_classification() -> None:
    """Catch a single position-difference episode being called stable evidence."""
    rows = pd.DataFrame(
        {
            "return_delta": [0.01] * 5,
            "sharpe_delta": [0.10] * 5,
            "dominant_share": [0.2, 0.2, 0.2, 0.2, 0.51],
        }
    )

    assert classify_annual_effect(rows, 0.001, 0.02, 4) == "inconclusive"


def test_position_difference_share_groups_contiguous_intervals() -> None:
    """Catch daily rows being treated as independent dominance episodes."""
    index = pd.bdate_range("2025-01-02", periods=6)
    champion = pd.Series([0, 1, 1, 0, 0, 0], index=index, dtype=float)
    counterfactual = pd.Series([0, 0, 0, 0, 1, 0], index=index, dtype=float)
    daily_delta = pd.Series([0, 0.02, 0.03, 0, 0.05, 0], index=index, dtype=float)

    assert dominant_difference_share(champion, counterfactual, daily_delta) == pytest.approx(0.5)


def test_signal_counterfactuals_do_not_mutate_inputs() -> None:
    """Catch attribution runs contaminating later counterfactuals."""
    index = pd.bdate_range("2025-01-02", periods=2)
    mapped = pd.DataFrame({"a": [1.0, -1.0], "b": [0.5, 0.5]}, index=index)
    original = mapped.copy()
    groups = {"structure": ("a", "b"), "trend": (), "volume_position": ()}

    zeroed = zero_signal_contribution(mapped, "a", pd.Series([True, False], index=index))
    dropped = drop_signal_and_reaggregate(mapped, groups, "a")

    pd.testing.assert_frame_equal(mapped, original)
    assert zeroed["a"].tolist() == [0.0, -1.0]
    assert dropped["structure"].tolist() == [0.5, 0.5]


def test_weight_delta_preserves_sum_and_other_rule_fields() -> None:
    """Catch sensitivity rules that change scale or unrelated state-machine fields."""
    champion = Rule((0.3, 0.3, 0.4), 0.15, 0.0, 1, 3, 1, "none")

    changed = rule_with_weight_delta(champion, 0, 0.05)

    assert changed.weights == pytest.approx((0.35, 0.275, 0.375))
    assert sum(changed.weights) == pytest.approx(1.0)
    assert changed.enter == champion.enter
    assert changed.exit == champion.exit
    assert changed.confirm_days == champion.confirm_days
    assert changed.min_hold_days == champion.min_hold_days
    assert changed.exit_confirm_days == champion.exit_confirm_days
