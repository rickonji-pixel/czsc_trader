from __future__ import annotations

import pandas as pd
import pytest
import numpy as np

from czsc_trader.rules import Rule
from czsc_trader.score_tier_diagnostic import diagnose_score_monotonicity
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


def _diagnostic_fixture(*, reverse_last_two_years: bool = False):
    dates = pd.DatetimeIndex(
        np.concatenate(
            [pd.bdate_range(f"{year}-01-04", periods=80).to_numpy() for year in range(2021, 2026)]
        ),
        name="dt",
    )
    scores = pd.Series(
        np.tile(np.r_[np.full(35, 0.05), np.full(10, 0.10), np.full(35, 0.20)], 5),
        index=dates,
        name="factor_score",
    )
    target = pd.Series(1.0, index=dates, name="target_position")
    event = pd.Series(0.0, index=dates, name="risk_event")
    high = scores.ge(0.175)
    returns = np.where(high, 0.05, 0.01)
    drawdowns = np.where(high, -0.03, -0.10)
    if reverse_last_two_years:
        reverse = dates.year >= 2024
        returns[reverse & high] = -0.03
        drawdowns[reverse & high] = -0.15
    outcomes = pd.DataFrame(index=dates)
    for horizon in (5, 10, 20):
        outcomes[f"return_{horizon}"] = returns
        outcomes[f"max_drawdown_{horizon}"] = drawdowns
    return scores, target, event, outcomes


def test_score_monotonicity_passes_stable_cross_year_effects() -> None:
    result = diagnose_score_monotonicity(*_diagnostic_fixture())

    assert result.summary["status"] == "PASS"
    assert result.summary["same_direction_years"] == 5
    assert result.hac_result["coefficient"] > 0
    assert result.hac_result["p_value"] <= 0.10
    assert (result.influence_audit["leave_one_year_out_drawdown_effect"] > 0).all()


def test_score_monotonicity_rejects_two_reversed_years() -> None:
    result = diagnose_score_monotonicity(
        *_diagnostic_fixture(reverse_last_two_years=True)
    )

    assert result.summary["status"] == "FAIL"
    assert result.summary["same_direction_years"] == 3


def test_score_tier_diagnostic_protocol_rejects_future_or_changed_boundaries() -> None:
    from czsc_trader.score_tier_diagnostic_runner import validate_protocol

    valid = {
        "experiment_id": "0901_EX16",
        "handler": "ex13_score_monotonicity_diagnostic",
        "symbol": "588080.SH",
        "visible_end": "2025-12-31",
        "access_2026": False,
        "challenger_experiment": "0901_EX13",
        "challenger_manifest_sha256": "7f381ff0e6cefffd77149cab93d759b46a1b6ef06d046b1169ffd3506a011589",
        "tier_boundaries": [0.025, 0.075, 0.125, 0.175],
        "minimum_annual_support": 30,
        "hac_max_lag": 20,
    }
    validate_protocol(valid)
    for key, value in (
        ("visible_end", "2026-08-28"),
        ("access_2026", True),
        ("tier_boundaries", [0.025, 0.10, 0.125, 0.175]),
        ("minimum_annual_support", 20),
    ):
        with pytest.raises(ValueError):
            validate_protocol({**valid, key: value})
