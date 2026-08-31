from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from czsc_trader.czsc_factor_stability import (
    build_forward_outcomes,
    event_onsets,
    evaluate_factor_stability,
    select_discovery_candidates,
    validate_frozen_candidates,
)
from czsc_trader.research.registry import build_default_registry


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_forward_outcomes_use_only_sessions_after_signal_date() -> None:
    index = pd.bdate_range("2021-01-04", periods=5)
    daily = pd.DataFrame(
        {"dt": index, "close": [100.0, 90.0, 120.0, 108.0, 130.0]}
    )

    outcomes = build_forward_outcomes(daily, index, (2,))

    assert outcomes.loc[index[0], "return_2"] == pytest.approx(0.20)
    assert outcomes.loc[index[0], "mae_2"] == pytest.approx(-0.10)
    assert outcomes.loc[index[0], "max_drawdown_2"] == pytest.approx(-0.10)
    assert outcomes.loc[index[1], "return_2"] == pytest.approx(0.20)
    assert outcomes.loc[index[1], "mae_2"] == pytest.approx(0.20)
    assert outcomes.loc[index[1], "max_drawdown_2"] == pytest.approx(-0.10)
    assert np.isnan(outcomes.loc[index[-2], "return_2"])


def test_event_onsets_collapse_consecutive_active_days() -> None:
    index = pd.bdate_range("2021-01-04", periods=8)
    indicator = pd.Series([0, 1, 1, 0, 1, 1, 1, 0], index=index)

    onsets = event_onsets(indicator)

    assert onsets.tolist() == [False, True, False, False, True, False, False, False]


def test_factor_stability_compares_active_and_inactive_within_year() -> None:
    index = pd.to_datetime(
        ["2021-01-04", "2021-01-05", "2021-01-06", "2021-01-07"]
    )
    indicator = pd.Series([1, 0, 1, 0], index=index, name="factor_a")
    outcomes = pd.DataFrame(
        {
            "return_20": [0.10, -0.10, 0.20, -0.20],
            "mae_20": [-0.01, -0.08, -0.02, -0.09],
            "max_drawdown_20": [-0.02, -0.10, -0.03, -0.11],
        },
        index=index,
    )

    metrics = evaluate_factor_stability(
        indicator, outcomes, factor_kind="state", years=(2021,), horizons=(20,)
    )

    row = metrics.iloc[0]
    assert row["active_n"] == 2
    assert row["control_n"] == 2
    assert row["return_delta"] == pytest.approx(0.30)
    assert row["max_drawdown_delta"] == pytest.approx(0.08)


def test_discovery_selection_requires_same_direction_in_every_year() -> None:
    rows = []
    for factor, deltas in {
        "stable": (0.01, 0.02, 0.03),
        "reversal": (0.02, -0.01, 0.03),
    }.items():
        for year, delta in zip((2021, 2022, 2023), deltas, strict=True):
            rows.append(
                {
                    "factor": factor,
                    "factor_kind": "state",
                    "year": year,
                    "horizon": 20,
                    "active_n": 20,
                    "control_n": 100,
                    "return_delta": delta,
                    "max_drawdown_delta": 0.001,
                }
            )

    selected = select_discovery_candidates(
        pd.DataFrame(rows),
        years=(2021, 2022, 2023),
        primary_horizon=20,
        minimum_absolute_effect=0.005,
    )

    assert selected["factor"].tolist() == ["stable"]
    assert selected.loc[0, "endpoint"] == "return_delta"
    assert selected.loc[0, "direction"] == 1
    assert selected.loc[0, "discovery_effect"] == pytest.approx(0.02)


def test_validation_keeps_frozen_endpoint_and_rejects_yearly_reversal() -> None:
    candidates = pd.DataFrame(
        [
            {
                "factor": "stable",
                "factor_kind": "event",
                "endpoint": "max_drawdown_delta",
                "direction": -1,
                "discovery_effect": -0.02,
            },
            {
                "factor": "reversal",
                "factor_kind": "state",
                "endpoint": "return_delta",
                "direction": 1,
                "discovery_effect": 0.03,
            },
        ]
    )
    metrics = pd.DataFrame(
        [
            {"factor": "stable", "year": 2024, "horizon": 20, "max_drawdown_delta": -0.01},
            {"factor": "stable", "year": 2025, "horizon": 20, "max_drawdown_delta": -0.02},
            {"factor": "reversal", "year": 2024, "horizon": 20, "return_delta": 0.02},
            {"factor": "reversal", "year": 2025, "horizon": 20, "return_delta": -0.01},
        ]
    )

    validated = validate_frozen_candidates(
        metrics,
        candidates,
        years=(2024, 2025),
        primary_horizon=20,
        minimum_absolute_effect=0.005,
    )

    assert validated.set_index("factor")["pass"].to_dict() == {
        "stable": True,
        "reversal": False,
    }


def test_czsc_factor_stability_handler_is_registered() -> None:
    protocol = json.loads(
        (
            REPO_ROOT
            / "experiments"
            / "0901_EX01"
            / "artifacts"
            / "protocol.json"
        ).read_text(encoding="utf-8")
    )

    handler = build_default_registry().resolve(protocol, "0901_EX01")

    assert handler.handler_id == "czsc_factor_stability_diagnostic"
