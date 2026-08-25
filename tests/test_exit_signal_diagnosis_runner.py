from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from czsc_trader.exit_signal_diagnosis_runner import (
    build_continuous_feature_separation,
    classify_exit_quality,
    cliffs_delta,
    exit_concentration,
    label_exit,
    replay_exit_counterfactual,
    run_exit_signal_diagnosis,
    select_endpoint,
    summarize_contexts,
    validate_event_evidence,
    validate_protocol,
    validate_source_archive,
)


def _prices() -> pd.DataFrame:
    index = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06"])
    return pd.DataFrame(
        {
            "open": [100.0, 120.0, 110.0],
            "close": [110.0, 115.0, 121.0],
        },
        index=index,
    )


def test_reentry_counterfactual_includes_avoided_exit_and_entry_fees() -> None:
    """Catch re-entry replay omitting either transaction paid by the actual path."""
    summary, ledger = replay_exit_counterfactual(
        _prices(),
        pd.Timestamp("2025-01-02"),
        pd.Timestamp("2025-01-03"),
        "ex04_reentry_execution",
        fee_rate=0.01,
    )

    expected_actual = 0.99 / 1.01
    expected_continued = 1.2
    assert summary["actual_terminal_wealth"] == pytest.approx(expected_actual)
    assert summary["continued_terminal_wealth"] == pytest.approx(expected_continued)
    assert summary["counterfactual_log_advantage"] == pytest.approx(
        np.log(expected_continued / expected_actual)
    )
    assert ledger["incremental_log_advantage"].sum() == pytest.approx(
        summary["counterfactual_log_advantage"], abs=1e-15
    )
    assert ledger.iloc[-1]["valuation_point"] == "endpoint_open"


def test_baseline_exit_counterfactual_sells_continued_path_at_endpoint() -> None:
    """Catch a baseline-exit endpoint leaving the counterfactual position open."""
    summary, _ = replay_exit_counterfactual(
        _prices(),
        pd.Timestamp("2025-01-02"),
        pd.Timestamp("2025-01-03"),
        "baseline_exit_execution",
        fee_rate=0.01,
    )

    assert summary["actual_terminal_wealth"] == pytest.approx(0.99)
    assert summary["continued_terminal_wealth"] == pytest.approx(1.2 * 0.99)
    assert summary["counterfactual_log_advantage"] == pytest.approx(np.log(1.2))


def test_window_end_counterfactual_marks_continued_path_at_close() -> None:
    """Catch half-year truncation charging an unregistered terminal sale fee."""
    summary, ledger = replay_exit_counterfactual(
        _prices(),
        pd.Timestamp("2025-01-02"),
        pd.Timestamp("2025-01-06"),
        "window_end_close",
        fee_rate=0.01,
    )

    assert summary["actual_terminal_wealth"] == pytest.approx(0.99)
    assert summary["continued_terminal_wealth"] == pytest.approx(1.21)
    assert ledger.iloc[-1]["valuation_point"] == "endpoint_close"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0051, "false_exit"),
        (0.005, "neutral_exit"),
        (0.0, "neutral_exit"),
        (-0.005, "neutral_exit"),
        (-0.0051, "protective_exit"),
    ],
)
def test_exit_label_uses_strict_materiality_boundary(value: float, expected: str) -> None:
    """Catch exact boundary values being promoted to material outcomes."""
    assert label_exit(value, 0.005) == expected


def test_cliffs_delta_counts_pairwise_wins_losses_and_ties() -> None:
    """Catch ties being counted as wins or losses in continuous separation."""
    assert cliffs_delta([3.0, 2.0], [2.0, 1.0]) == pytest.approx(0.75)
    assert cliffs_delta([1.0, 1.0], [1.0, 1.0]) == 0.0


def _context_rules() -> dict[str, object]:
    return {
        "minimum_non_neutral_events": 4,
        "minimum_windows": 2,
        "minimum_target_rate": 0.75,
        "maximum_single_event_share": 0.5,
    }


def test_context_support_requires_rate_windows_net_sign_and_diversification() -> None:
    """Catch a small or single-event context being declared an exit-quality signal."""
    events = pd.DataFrame(
        {
            "window": ["2021H1", "2021H1", "2023H1", "2023H1", "2025H2"],
            "regime": ["uptrend"] * 4 + ["downtrend"],
            "outcome_label": [
                "false_exit",
                "false_exit",
                "false_exit",
                "protective_exit",
                "protective_exit",
            ],
            "counterfactual_log_advantage": [0.02, 0.015, 0.01, -0.01, -0.02],
        }
    )

    result = summarize_contexts(events, ("regime",), _context_rules())
    uptrend = result.set_index("axis_value").loc["uptrend"]

    assert uptrend["non_neutral_events"] == 4
    assert uptrend["window_count"] == 2
    assert uptrend["false_exit_rate"] == pytest.approx(0.75)
    assert bool(uptrend["false_exit_context"])
    assert not bool(uptrend["protective_exit_context"])


def test_context_fails_when_one_event_exceeds_half_of_target_contribution() -> None:
    """Catch nominal support whose result is dominated by one exit event."""
    events = pd.DataFrame(
        {
            "window": ["2021H1", "2021H1", "2023H1", "2023H1"],
            "block_label": ["joint"] * 4,
            "outcome_label": ["false_exit"] * 4,
            "counterfactual_log_advantage": [0.07, 0.01, 0.01, 0.01],
        }
    )

    result = summarize_contexts(events, ("block_label",), _context_rules())

    assert result.iloc[0]["false_exit_max_single_share"] == pytest.approx(0.7)
    assert not bool(result.iloc[0]["false_exit_context"])


def _protocol() -> dict[str, object]:
    return json.loads(
        Path("experiments/0825_EX04/artifacts/protocol.json").read_text(encoding="utf-8")
    )


def _classification_events(
    labels: list[str], advantages: list[float], windows: list[str]
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "event_id": [f"E{i}" for i in range(len(labels))],
            "window": windows,
            "outcome_label": labels,
            "counterfactual_log_advantage": advantages,
        }
    )


def _pad_to_twenty(events: pd.DataFrame) -> pd.DataFrame:
    rows = [events]
    for number in range(len(events), 20):
        rows.append(
            pd.DataFrame(
                [
                    {
                        "event_id": f"N{number}",
                        "window": "2025H1",
                        "outcome_label": "neutral_exit",
                        "counterfactual_log_advantage": 0.0,
                    }
                ]
            )
        )
    return pd.concat(rows, ignore_index=True)


def test_concentration_uses_false_exit_positive_advantages_only() -> None:
    """Catch protective benefits entering the wrong-exit concentration denominator."""
    events = _classification_events(
        ["false_exit", "false_exit", "false_exit", "protective_exit"],
        [0.04, 0.02, 0.01, -1.0],
        ["2021H1", "2023H1", "2025H2", "2022H1"],
    )

    result = exit_concentration(events, {"top1_share": 0.6, "top3_share": 1.1})

    assert result["total_false_exit_log_loss"] == pytest.approx(0.07)
    assert result["top1_share"] == pytest.approx(4 / 7)
    assert not result["concentrated"]


def test_machine_classification_branches_are_applied_in_registered_order() -> None:
    """Catch a later context rule overriding insufficient or concentrated evidence."""
    protocol = _protocol()
    base = _pad_to_twenty(
        _classification_events(
            ["false_exit"] * 4 + ["protective_exit"] * 4,
            [0.02, 0.02, 0.02, 0.02, -0.02, -0.02, -0.02, -0.02],
            ["2021H1", "2023H1", "2025H2", "2021H2"] * 2,
        )
    )
    empty_contexts = pd.DataFrame(
        columns=["false_exit_context", "protective_exit_context"]
    )
    empty_features = pd.DataFrame(columns=["stable_large_effect"])

    insufficient = classify_exit_quality(
        base.loc[base["outcome_label"].ne("protective_exit")],
        empty_contexts,
        empty_features,
        protocol,
    )
    assert insufficient["classification"] == "insufficient_exit_evidence"

    concentrated_protocol = copy.deepcopy(protocol)
    concentrated_protocol["concentration"] = {"top1_share": 0.2, "top3_share": 0.5}
    concentrated = classify_exit_quality(
        base, empty_contexts, empty_features, concentrated_protocol
    )
    assert concentrated["classification"] == "path_concentrated_exit_failure"

    context_rows = pd.DataFrame(
        [{"false_exit_context": True, "protective_exit_context": True}]
    )
    context_protocol = copy.deepcopy(protocol)
    context_protocol["concentration"] = {"top1_share": 0.9, "top3_share": 1.1}
    context = classify_exit_quality(base, context_rows, empty_features, context_protocol)
    assert context["classification"] == "context_dependent_exit_quality"

    mixed = classify_exit_quality(base, empty_contexts, empty_features, context_protocol)
    assert mixed["classification"] == "mixed_exit_quality"


def test_machine_classifies_systematically_poor_before_context_dependence() -> None:
    """Catch context rows masking a globally poor exit signal."""
    windows = [
        "2021H1",
        "2021H2",
        "2022H1",
        "2022H2",
        "2023H1",
        "2023H2",
        "2024H1",
        "2024H2",
    ]
    events = _pad_to_twenty(
        _classification_events(
            ["false_exit"] * 9 + ["protective_exit"] * 3,
            [0.01] * 9 + [-0.01] * 3,
            windows + ["2021H1"] + ["2025H2"] * 3,
        )
    )
    contexts = pd.DataFrame(
        [{"false_exit_context": True, "protective_exit_context": True}]
    )
    features = pd.DataFrame([{"stable_large_effect": True}])
    protocol = copy.deepcopy(_protocol())
    protocol["concentration"] = {"top1_share": 0.9, "top3_share": 1.1}

    result = classify_exit_quality(events, contexts, features, protocol)

    assert result["classification"] == "systematically_poor_exit_signal"


@pytest.mark.parametrize(
    ("dotted", "value", "message"),
    [
        ("holdout_access_allowed", True, "holdout"),
        ("promotion.optimize_parameters", True, "promotion"),
        ("expected_event_count", 19, "20"),
        ("visible_sample_end", "2026-01-01", "2025-12-31"),
    ],
)
def test_protocol_rejects_boundary_drift(
    dotted: str, value: object, message: str
) -> None:
    """Catch formal diagnosis widening into selection or holdout access."""
    protocol = copy.deepcopy(_protocol())
    target: dict[str, object] = protocol
    parts = dotted.split(".")
    for part in parts[:-1]:
        target = target[part]  # type: ignore[assignment]
    target[parts[-1]] = value

    with pytest.raises(ValueError, match=message):
        validate_protocol(protocol)


def test_event_evidence_rejects_duplicates_2026_and_wrong_count() -> None:
    """Catch source-event drift before prices or outcomes are evaluated."""
    events = pd.DataFrame(
        {
            "event_id": ["same", "same"],
            "signal_date": ["2025-01-01", "2026-01-01"],
            "event_type": ["early_ex04_exit", "early_ex04_exit"],
        }
    )

    with pytest.raises(ValueError, match="count|duplicate|2026"):
        validate_event_evidence(events, expected_count=20)


def test_source_archive_uses_portable_manifest_hashes() -> None:
    """Catch EX04 binding to device-specific checkout bytes instead of tracked evidence."""
    source = copy.deepcopy(_protocol()["source_archive"])

    validated = validate_source_archive(Path("."), source)

    assert validated["experiment_id"] == "0825_EX03"
    source["files"]["artifacts/decision_events.csv"] = "0" * 64
    with pytest.raises(ValueError, match="source archive.*hash"):
        validate_source_archive(Path("."), source)


def test_endpoint_selects_first_state_convergence_and_registered_tie_priority() -> None:
    """Catch event replay extending beyond the first causal convergence date."""
    dates = pd.to_datetime(
        ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"]
    )
    ledger = pd.DataFrame(
        {
            "date": dates,
            "baseline_execution_position": [1.0, 1.0, 1.0, 0.0],
            "ex04_execution_position": [1.0, 0.0, 1.0, 0.0],
        }
    )

    endpoint = select_endpoint(
        ledger,
        pd.Timestamp("2025-01-03"),
        ("ex04_reentry_execution", "baseline_exit_execution", "window_end_close"),
    )

    assert endpoint == (pd.Timestamp("2025-01-06"), "ex04_reentry_execution")

    tied = ledger.copy()
    tied.loc[tied["date"].eq(pd.Timestamp("2025-01-06")), "baseline_execution_position"] = 0.0
    assert select_endpoint(
        tied,
        pd.Timestamp("2025-01-03"),
        ("ex04_reentry_execution", "baseline_exit_execution", "window_end_close"),
    ) == (pd.Timestamp("2025-01-06"), "ex04_reentry_execution")


def test_continuous_feature_requires_large_stable_cross_window_separation() -> None:
    """Catch a pooled effect with inconsistent window direction being called stable."""
    events = pd.DataFrame(
        {
            "window": ["2021H1"] * 4 + ["2023H1"] * 4,
            "outcome_label": ["false_exit"] * 2 + ["protective_exit"] * 2
            + ["false_exit"] * 2
            + ["protective_exit"] * 2,
            "feature": [4.0, 3.0, 2.0, 1.0, 8.0, 7.0, 6.0, 5.0],
        }
    )
    rules = {
        "minimum_events_per_class": 4,
        "minimum_comparable_windows": 2,
        "large_cliffs_delta": 0.474,
        "require_consistent_window_direction": True,
    }

    result = build_continuous_feature_separation(events, ("feature",), rules)

    assert result.iloc[0]["cliffs_delta"] == pytest.approx(0.5)
    assert result.iloc[0]["comparable_windows"] == 2
    assert bool(result.iloc[0]["stable_large_effect"])

    events.loc[events["window"].eq("2023H1"), "feature"] = [5.0, 6.0, 7.0, 8.0]
    inconsistent = build_continuous_feature_separation(events, ("feature",), rules)
    assert not bool(inconsistent.iloc[0]["stable_large_effect"])


def test_formal_runner_validates_protocol_before_touching_inputs(tmp_path: Path) -> None:
    """Catch orchestration reading source evidence before enforcing no-holdout rules."""
    protocol = copy.deepcopy(_protocol())
    protocol["holdout_access_allowed"] = True

    with pytest.raises(ValueError, match="holdout"):
        run_exit_signal_diagnosis(tmp_path, tmp_path, protocol)
