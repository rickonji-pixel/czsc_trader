import numpy as np
import pandas as pd

from czsc_trader.rules import (
    Rule,
    apply_fixed_rule,
    build_factor_events,
    positions_for_rule,
)


def test_rule_state_machine_confirms_entry_and_minimum_hold() -> None:
    index = pd.bdate_range("2025-01-02", periods=7)
    factors = pd.DataFrame(
        {
            "structure": [0.5, 0.5, 0.5, -0.5, -0.5, -0.5, -0.5],
            "trend": [0.5, 0.5, 0.5, -0.5, -0.5, -0.5, -0.5],
            "volume_position": [0.5, 0.5, 0.5, -0.5, -0.5, -0.5, -0.5],
        },
        index=index,
    )
    rule = Rule((0.4, 0.4, 0.2), enter=0.2, exit=0.0, confirm_days=2, min_hold_days=3)

    positions, scores = positions_for_rule(factors, rule)

    assert scores.tolist() == [0.5, 0.5, 0.5, -0.5, -0.5, -0.5, -0.5]
    assert positions.tolist() == [0.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0]


def test_entry_gate_blocks_positive_score_without_selected_factor_confirmation() -> None:
    index = pd.bdate_range("2025-01-02", periods=2)
    factors = pd.DataFrame(
        {
            "structure": [1.0, 1.0],
            "trend": [-0.5, 0.0],
            "volume_position": [1.0, 1.0],
        },
        index=index,
    )
    rule = Rule(
        (0.4, 0.4, 0.2),
        enter=0.2,
        exit=0.0,
        confirm_days=1,
        min_hold_days=1,
        entry_gate="trend",
    )

    positions, scores = positions_for_rule(factors, rule)

    np.testing.assert_allclose(scores.to_numpy(), [0.4, 0.6])
    assert positions.tolist() == [0.0, 1.0]


def test_exit_confirmation_resets_when_score_recovers() -> None:
    index = pd.bdate_range("2025-01-02", periods=5)
    values = [0.5, -0.5, 0.5, -0.5, -0.5]
    factors = pd.DataFrame(
        {column: values for column in ("structure", "trend", "volume_position")},
        index=index,
    )
    rule = Rule(
        (0.4, 0.4, 0.2),
        enter=0.2,
        exit=0.0,
        confirm_days=1,
        min_hold_days=1,
        exit_confirm_days=2,
    )

    positions, _ = positions_for_rule(factors, rule)

    assert positions.tolist() == [1.0, 1.0, 1.0, 1.0, 0.0]


def test_factor_events_are_the_only_position_transitions() -> None:
    index = pd.bdate_range("2025-01-02", periods=6)
    factors = pd.DataFrame(
        {
            "structure": [0.5, 0.5, -0.5, -0.5, 0.5, 0.5],
            "trend": [0.5, 0.5, -0.5, -0.5, 0.5, 0.5],
            "volume_position": [0.5, 0.5, -0.5, -0.5, 0.5, 0.5],
        },
        index=index,
    )
    rule = Rule((0.4, 0.4, 0.2), enter=0.2, exit=0.0, confirm_days=1, min_hold_days=1)
    target, scores = positions_for_rule(factors, rule)

    events = build_factor_events(target, scores, factors, rule)

    assert events["event_type"].tolist() == ["Entry", "Exit", "Entry"]
    assert events["after_position"].tolist() == [1.0, 0.0, 1.0]
    assert events["signal_date"].tolist() == [index[0], index[2], index[4]]


def test_apply_fixed_rule_returns_positions_scores_and_events() -> None:
    index = pd.bdate_range("2026-01-02", periods=4)
    values = [0.5, 0.5, -0.5, -0.5]
    factors = pd.DataFrame(
        {column: values for column in ("structure", "trend", "volume_position")},
        index=index,
    )
    rule = Rule((0.3, 0.3, 0.4), 0.15, 0.0, 1, 1)

    result = apply_fixed_rule(factors, rule)

    assert result.target_position.tolist() == [1.0, 1.0, 0.0, 0.0]
    assert result.scores.tolist() == values
    assert result.events["event_type"].tolist() == ["Entry", "Exit"]
    assert result.events["signal_date"].tolist() == [index[0], index[2]]
