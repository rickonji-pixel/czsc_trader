from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.czsc_state_age import (
    classify_age_relationship,
    consecutive_state_age,
)
from czsc_trader.research.registry import build_default_registry


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_consecutive_state_age_resets_and_increments_causally() -> None:
    index = pd.bdate_range("2021-01-04", periods=8)
    state = pd.Series([0, 1, 1, 1, 0, 1, 1, 0], index=index)

    age = consecutive_state_age(state)

    assert age.tolist() == [0, 1, 2, 3, 0, 1, 2, 0]


def test_age_relationship_requires_same_sign_and_minimum_median_strength() -> None:
    stable = pd.DataFrame(
        {
            "year": [2021, 2022, 2023, 2024, 2025],
            "horizon": [20] * 5,
            "max_drawdown_rho": [0.11, 0.20, 0.15, 0.12, 0.10],
        }
    )
    reversal = stable.copy()
    reversal.loc[4, "max_drawdown_rho"] = -0.10

    assert classify_age_relationship(stable, primary_horizon=20, minimum_abs_median_rho=0.10) == "stable_age_relationship"
    assert classify_age_relationship(reversal, primary_horizon=20, minimum_abs_median_rho=0.10) == "state_effect_not_explained_by_age"


def test_state_age_handler_is_registered() -> None:
    protocol = json.loads(
        (REPO_ROOT / "experiments" / "0901_EX03" / "artifacts" / "protocol.json").read_text(encoding="utf-8")
    )

    handler = build_default_registry().resolve(protocol, "0901_EX03")

    assert handler.handler_id == "czsc_state_age_diagnosis"
