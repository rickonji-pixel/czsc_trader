from __future__ import annotations

import pandas as pd
import pytest

from czsc_trader.rules import Rule
from czsc_trader.score_tier_position import (
    build_tier_mapping,
    classify_score_tiers,
    positions_from_score_tiers,
)


def _rule() -> Rule:
    return Rule(
        weights=(0.0, 0.0, 0.0),
        enter=0.175,
        exit=0.025,
        confirm_days=1,
        min_hold_days=3,
        exit_confirm_days=1,
        entry_gate="none",
    )


def test_classify_score_tiers_uses_frozen_boundaries() -> None:
    scores = pd.Series(
        [-0.1, 0.025, 0.026, 0.074, 0.075, 0.124, 0.125, 0.174, 0.175]
    )

    assert classify_score_tiers(scores).tolist() == [0, 0, 1, 1, 2, 2, 3, 3, 4]


def test_only_four_preregistered_mappings_exist() -> None:
    assert build_tier_mapping("M1") == {
        0: 0.0,
        1: 0.5,
        2: 0.75,
        3: 1.0,
        4: 1.0,
    }
    with pytest.raises(ValueError, match="unknown score-tier mapping"):
        build_tier_mapping("M5")


def test_resize_requires_two_completed_signal_days_and_exit_is_immediate() -> None:
    scores = pd.Series([0.20, 0.20, 0.20, 0.05, 0.05, 0.20, 0.20, 0.01])

    target = positions_from_score_tiers(scores, _rule(), "M1")

    assert target.tolist() == [1, 1, 1, 1, 0.5, 0.5, 1, 0]


def test_fractional_target_never_uses_next_score() -> None:
    scores = pd.Series([0.20, 0.20, 0.08, 0.08, 0.20, 0.01])

    prefix = positions_from_score_tiers(scores.iloc[:-1], _rule(), "M2")
    full = positions_from_score_tiers(scores, _rule(), "M2")

    assert prefix.equals(full.iloc[:-1])
