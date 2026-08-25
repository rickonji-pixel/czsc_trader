from __future__ import annotations

import copy
import json
from pathlib import Path

import pandas as pd
import pytest

from czsc_trader.decision_boundary_runner import (
    build_frontier_events,
    classify_boundary,
    enumerate_boundary_rules,
    run_leave_one_year_out,
    select_training_rule,
    validate_protocol,
)


def _protocol() -> dict[str, object]:
    return json.loads(
        Path("experiments/0826_EX01/artifacts/protocol.json").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"holdout_access_allowed": True}, "holdout"),
        ({"visible_sample_end": "2026-01-01"}, "2025-12-31"),
        ({"experiment_type": "strategy_search"}, "decision boundary"),
        ({"promotion": {"run_holdout": True}}, "promotion"),
    ],
)
def test_protocol_rejects_holdout_or_promotion_drift(
    mutation: dict[str, object], message: str
) -> None:
    protocol = copy.deepcopy(_protocol())
    protocol.update(mutation)

    with pytest.raises(ValueError, match=message):
        validate_protocol(protocol)


def _ledger() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "window": "2021H1",
                "date": "2021-01-04",
                "baseline_execution_position": 0.0,
                "ex04_execution_position": 0.0,
                "path_mechanism": "both_cash",
                "log_wealth_delta": 0.0,
            },
            {
                "window": "2021H1",
                "date": "2021-01-05",
                "baseline_execution_position": 1.0,
                "ex04_execution_position": 0.0,
                "path_mechanism": "late_entry",
                "log_wealth_delta": -0.020,
            },
            {
                "window": "2021H1",
                "date": "2021-01-06",
                "baseline_execution_position": 1.0,
                "ex04_execution_position": 0.0,
                "path_mechanism": "late_entry",
                "log_wealth_delta": 0.005,
            },
            {
                "window": "2021H1",
                "date": "2021-01-07",
                "baseline_execution_position": 1.0,
                "ex04_execution_position": 1.0,
                "path_mechanism": "both_long",
                "log_wealth_delta": 0.0,
            },
            {
                "window": "2023H2",
                "date": "2023-07-03",
                "baseline_execution_position": 1.0,
                "ex04_execution_position": 0.0,
                "path_mechanism": "late_entry",
                "log_wealth_delta": -0.002,
            },
            {
                "window": "2023H2",
                "date": "2023-07-04",
                "baseline_execution_position": 1.0,
                "ex04_execution_position": 1.0,
                "path_mechanism": "both_long",
                "log_wealth_delta": 0.0,
            },
        ]
    )


def _events() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "event_id": "2021H1:missed_baseline_entry:20210104",
                "window": "2021H1",
                "signal_date": "2021-01-04",
                "execution_date": "2021-01-05",
                "event_type": "missed_baseline_entry",
                "regime": "uptrend",
                "block_label": "threshold_block",
                "ex04_margin": -0.010,
            }
        ]
    )


def test_frontier_builder_uses_one_interval_and_fee_inclusive_value() -> None:
    events, excluded = build_frontier_events(_ledger(), _events(), _protocol())

    assert events["event_id"].to_list() == ["2021H1:missed_baseline_entry:20210104"]
    assert events["start_date"].astype(str).to_list() == ["2021-01-05"]
    assert events["end_date"].astype(str).to_list() == ["2021-01-06"]
    assert events["interval_days"].to_list() == [2]
    assert events["alternative_log_value"].to_list() == pytest.approx([0.015])
    assert events["action_family"].to_list() == ["enter_now"]
    assert events["margin_band"].to_list() == ["near"]
    assert excluded[["window", "start_date", "reason"]].astype(str).to_dict("records") == [
        {
            "window": "2023H2",
            "start_date": "2023-07-03",
            "reason": "missing_causal_event",
        }
    ]


def test_frontier_builder_rejects_duplicate_or_unused_decision_events() -> None:
    duplicated = pd.concat([_events(), _events()], ignore_index=True)
    with pytest.raises(ValueError, match="duplicate|one-to-one"):
        build_frontier_events(_ledger(), duplicated, _protocol())

    unused = pd.concat(
        [
            _events(),
            pd.DataFrame(
                [
                    {
                        "event_id": "unused",
                        "window": "2021H1",
                        "signal_date": "2021-02-01",
                        "execution_date": "2021-02-02",
                        "event_type": "early_ex04_exit",
                        "regime": "sideways",
                        "block_label": "joint_margin_block",
                        "ex04_margin": -0.001,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    with pytest.raises(ValueError, match="unused"):
        build_frontier_events(_ledger(), unused, _protocol())


def test_frontier_builder_rejects_noninitial_interval_without_event() -> None:
    ledger = _ledger().copy()
    ledger.loc[ledger["date"].eq("2021-01-05"), "date"] = "2021-01-08"

    with pytest.raises(ValueError, match="missing causal event"):
        build_frontier_events(ledger, _events(), _protocol())


def test_rule_library_is_fixed_and_outcome_independent() -> None:
    rules = enumerate_boundary_rules(_protocol())

    assert len(rules) == 49
    assert rules["rule_id"].is_unique
    assert rules.iloc[0]["rule_id"] == "always_ex04"
    assert set(rules["complexity"].astype(int)) == {0, 1, 2, 3}
    assert not any("value" in column or "date" in column for column in rules.columns)


def _selection_events() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for year in range(2021, 2026):
        for number in range(2):
            rows.append(
                {
                    "event_id": f"{year}-entry-{number}",
                    "year": year,
                    "action_family": "enter_now",
                    "regime": "uptrend",
                    "block_label": "threshold_block",
                    "margin_band": "near",
                    "alternative_log_value": 0.01,
                }
            )
        rows.append(
            {
                "event_id": f"{year}-hold",
                "year": year,
                "action_family": "continue_hold",
                "regime": "downtrend",
                "block_label": "joint_margin_block",
                "margin_band": "far",
                "alternative_log_value": -0.02,
            }
        )
    return pd.DataFrame(rows)


def test_fold_selection_cannot_read_held_out_outcomes() -> None:
    events = _selection_events()
    rules = enumerate_boundary_rules(_protocol())
    original = select_training_rule(events, rules, 2025, _protocol())
    changed = events.copy()
    changed.loc[changed["year"].eq(2025), "alternative_log_value"] = -1000.0

    repeated = select_training_rule(changed, rules, 2025, _protocol())

    assert repeated["selected_rule_id"] == original["selected_rule_id"]
    assert repeated["training_pooled_mean"] == pytest.approx(original["training_pooled_mean"])


def test_fold_selection_requires_support_in_every_training_year() -> None:
    events = _selection_events()
    events.loc[
        events["event_id"].str.startswith("2024-entry"), "action_family"
    ] = "continue_hold"
    rules = enumerate_boundary_rules(_protocol())

    selected = select_training_rule(events, rules, 2025, _protocol())

    assert selected["selected_rule_id"] != "action_family=enter_now"


def _passing_folds_and_predictions() -> tuple[pd.DataFrame, pd.DataFrame]:
    folds = pd.DataFrame(
        {
            "held_out_year": list(range(2021, 2026)),
            "selected_rule_id": ["action_family=enter_now"] * 5,
            "selected_is_fallback": [False] * 5,
        }
    )
    rows = []
    for year in range(2021, 2026):
        for number in range(2):
            rows.append(
                {
                    "held_out_year": year,
                    "event_id": f"{year}-{number}",
                    "matched": True,
                    "alternative_log_value": 0.01,
                }
            )
    return folds, pd.DataFrame(rows)


def test_stability_classifier_accepts_only_a_five_year_nonconcentrated_boundary() -> None:
    folds, predictions = _passing_folds_and_predictions()

    result = classify_boundary(folds, predictions, _protocol())

    assert result["classification"] == "stable_boundary_found"
    assert result["annual_sign_test_pvalue"] == pytest.approx(0.03125)
    assert all(result["gates"].values())


@pytest.mark.parametrize(
    "mutation",
    ["rule_instability", "negative_year", "low_support", "concentration"],
)
def test_stability_classifier_rejects_each_weak_evidence_pattern(mutation: str) -> None:
    folds, predictions = _passing_folds_and_predictions()
    if mutation == "rule_instability":
        folds.loc[folds["held_out_year"].isin([2024, 2025]), "selected_rule_id"] = [
            "rule_b",
            "rule_c",
        ]
    elif mutation == "negative_year":
        predictions.loc[predictions["held_out_year"].eq(2025), "alternative_log_value"] = -0.01
    elif mutation == "low_support":
        predictions = predictions.iloc[:9].copy()
    elif mutation == "concentration":
        predictions.loc[predictions.index[0], "alternative_log_value"] = 1.0

    result = classify_boundary(folds, predictions, _protocol())

    assert result["classification"] == "no_stable_boundary"
    assert not all(result["gates"].values())


def test_leave_one_year_out_emits_only_held_out_predictions() -> None:
    events = _selection_events()
    folds, predictions = run_leave_one_year_out(
        events, enumerate_boundary_rules(_protocol()), _protocol()
    )

    assert folds["held_out_year"].to_list() == list(range(2021, 2026))
    assert set(predictions["held_out_year"].astype(int)) == set(range(2021, 2026))
    assert predictions["event_id"].str.slice(0, 4).eq(
        predictions["held_out_year"].astype(str)
    ).all()
