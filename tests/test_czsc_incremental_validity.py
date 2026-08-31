from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.czsc_factor_stability import classify_yearly_effects
from czsc_trader.research.registry import build_default_registry


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_incremental_effect_classification_requires_five_same_sign_years() -> None:
    stable = pd.Series([0.006, 0.010, 0.008, 0.007, 0.009])
    reversal = stable.copy()
    reversal.iloc[-1] = -0.009

    assert classify_yearly_effects(stable, minimum_abs_median=0.005) == "stable_incremental_validity"
    assert classify_yearly_effects(reversal, minimum_abs_median=0.005) == "no_stable_incremental_validity"


def test_incremental_validity_handler_is_registered() -> None:
    protocol = json.loads(
        (REPO_ROOT / "experiments" / "0901_EX04" / "artifacts" / "protocol.json").read_text(encoding="utf-8")
    )

    handler = build_default_registry().resolve(protocol, "0901_EX04")

    assert handler.handler_id == "czsc_incremental_validity_diagnosis"
