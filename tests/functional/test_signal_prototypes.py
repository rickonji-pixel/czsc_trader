from __future__ import annotations

import pandas as pd

from czsc_trader.signal_prototypes import build_minimal_prototype, two_by_two_effects


def test_minimal_prototype_applies_risk_and_time_exits() -> None:
    dates = pd.date_range("2026-01-05", periods=9, freq="B")
    entry = pd.Series(
        [None, "入", "入", "无", "入", "入", "入", "入", "无"],
        index=dates,
    )
    risk = pd.Series(
        ["无", "无", "险", "无", "无", "无", "无", "无", "无"],
        index=dates,
    )

    result = build_minimal_prototype(
        entry,
        "入",
        [("风险", risk, "险")],
        max_holding_sessions=2,
    )

    assert result.target_position.tolist() == [0, 1, 0, 0, 1, 1, 0, 0, 0]
    assert result.decisions["action"].tolist() == [
        "HOLD_CASH",
        "ENTER",
        "EXIT_RISK",
        "HOLD_CASH",
        "ENTER",
        "HOLD_POSITION",
        "EXIT_TIME",
        "HOLD_CASH",
        "HOLD_CASH",
    ]


def test_two_by_two_effects_separate_main_and_interaction_terms() -> None:
    effects = two_by_two_effects(10.0, 12.0, 13.0, 18.0)

    assert effects == {
        "top_divergence_main": 3.5,
        "structure_pressure_main": 4.5,
        "interaction": 3.0,
    }
