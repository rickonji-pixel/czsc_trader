from __future__ import annotations

import copy
import json
from pathlib import Path

import pandas as pd
import pytest

from czsc_trader.ex04_path_attribution_runner import (
    build_baseline_episode_table,
    classify_mechanism,
    classify_score_block,
    label_baseline_only_days,
    positions_from_dual_scores,
    reject_holdout_hashes,
    run_ex04_path_attribution,
    validate_artifact_identity,
    validate_log_wealth_closure,
    validate_path_ledger,
    validate_protocol,
    validate_source_evidence,
)
from czsc_trader.rules import Rule


def test_dual_scores_use_entry_source_only_while_flat_and_exit_source_only_while_long() -> None:
    """Catch hybrid state machines splicing frozen positions instead of evolving causally."""
    index = pd.date_range("2025-01-01", periods=7, freq="D")
    entry = pd.Series([0.2, 0.0, 0.0, 0.2, 0.2, 0.0, 0.0], index=index)
    exit_ = pd.Series([1.0, -1.0, -1.0, 1.0, -1.0, -1.0, -1.0], index=index)
    rule = Rule((0.3, 0.3, 0.4), 0.15, 0.0, 1, 2, 1, "none")

    result = positions_from_dual_scores(entry, 0.15, exit_, 0.0, rule)

    assert result.to_list() == [1.0, 1.0, 0.0, 1.0, 1.0, 0.0, 0.0]


def test_baseline_only_days_distinguish_late_missed_early_and_interrupted() -> None:
    """Catch distinct opportunity-loss paths being collapsed into generic lower exposure."""
    index = pd.date_range("2025-01-01", periods=14, freq="D")
    baseline = pd.Series(
        [1, 1, 1, 1, 0, 1, 1, 1, 0, 1, 1, 1, 1, 1], index=index, dtype=float
    )
    ex04 = pd.Series(
        [0, 0, 1, 1, 0, 0, 0, 0, 0, 1, 0, 1, 0, 0], index=index, dtype=float
    )

    episodes, labels = label_baseline_only_days(baseline, ex04)

    assert labels.to_list() == [
        "late_entry",
        "late_entry",
        "both_long",
        "both_long",
        "both_cash",
        "fully_missed_entry",
        "fully_missed_entry",
        "fully_missed_entry",
        "both_cash",
        "both_long",
        "interrupted_holding",
        "both_long",
        "early_exit",
        "early_exit",
    ]
    assert episodes["episode_class"].to_list() == [
        "late_entry",
        "fully_missed_entry",
        "interrupted_holding+early_exit",
    ]
    assert episodes["ex04_segment_count"].to_list() == [1, 0, 2]


def test_episode_table_joins_path_contributions_by_stable_episode_id() -> None:
    """Catch numeric episode IDs being joined to window-qualified ledger IDs."""
    episodes = pd.DataFrame(
        [
            {
                "episode_id": 1,
                "start": pd.Timestamp("2025-01-02"),
                "end": pd.Timestamp("2025-01-03"),
                "trading_days": 2,
                "ex04_overlap_days": 1,
                "ex04_segment_count": 1,
                "episode_class": "late_entry",
            }
        ]
    )
    ledger = pd.DataFrame(
        {
            "baseline_episode_id": ["2025H1:E001", "2025H1:E001"],
            "log_wealth_delta": [-0.02, 0.01],
        }
    )

    result = build_baseline_episode_table(episodes, ledger, "2025H1")

    assert result["baseline_episode_id"].to_list() == ["2025H1:E001"]
    assert result["episode_log_wealth_delta"].to_list() == pytest.approx([-0.01])
    assert result["episode_negative_log_loss"].to_list() == pytest.approx([0.02])


@pytest.mark.parametrize(
    ("kind", "baseline_score", "ex04_score", "expected"),
    [
        ("entry", 0.18, 0.14, "weight_block"),
        ("entry", 0.16, 0.16, "threshold_block"),
        ("entry", 0.16, 0.14, "independent_double_block"),
        ("entry", 0.18, 0.16, "joint_margin_block"),
        ("entry", 0.18, 0.18, "state_path_not_score"),
        ("exit", 0.03, -0.01, "weight_block"),
        ("exit", 0.01, 0.02, "threshold_block"),
        ("exit", 0.01, -0.01, "independent_double_block"),
        ("exit", 0.03, 0.01, "joint_margin_block"),
        ("exit", 0.03, 0.03, "state_path_not_score"),
    ],
)
def test_score_block_classification_is_literal_and_symmetric(
    kind: str, baseline_score: float, ex04_score: float, expected: str
) -> None:
    """Catch weights and thresholds receiving credit for a state-path difference."""
    result = classify_score_block(
        kind,
        baseline_score,
        ex04_score,
        baseline_enter=0.15,
        ex04_enter=0.175,
        baseline_exit=0.0,
        ex04_exit=0.025,
    )

    assert result == expected


def test_log_wealth_closure_matches_compounded_window_returns() -> None:
    """Catch daily path contributions that do not reconcile to terminal wealth."""
    baseline = pd.Series([0.1, -0.05])
    ex04 = pd.Series([0.0, 0.1])
    baseline_total = (1.1 * 0.95) - 1.0
    ex04_total = 0.1

    residual = validate_log_wealth_closure(
        baseline, ex04, baseline_total, ex04_total, tolerance=1e-12
    )

    assert residual == pytest.approx(0.0, abs=1e-15)


def _classification_protocol() -> dict[str, object]:
    return json.loads(
        Path("experiments/0825_EX03/artifacts/protocol.json").read_text(encoding="utf-8")
    )


def _mechanism_rows(entry_share: float, exit_share: float) -> pd.DataFrame:
    rows = []
    for window in ("2021H1", "2023H1", "2025H2"):
        rows.extend(
            [
                {"window": window, "mechanism": "late_entry", "negative_log_loss": entry_share},
                {"window": window, "mechanism": "early_exit", "negative_log_loss": exit_share},
            ]
        )
    return pd.DataFrame(rows)


def _hybrid_rows(entry_improvements: list[float], exit_improvements: list[float]) -> pd.DataFrame:
    windows = ("2021H1", "2023H1", "2025H2")
    rows = []
    for window, value in zip(windows, entry_improvements, strict=True):
        rows.append(
            {
                "window": window,
                "variant": "baseline_entry_ex04_exit",
                "return_delta_vs_ex04": value,
            }
        )
    for window, value in zip(windows, exit_improvements, strict=True):
        rows.append(
            {
                "window": window,
                "variant": "ex04_entry_baseline_exit",
                "return_delta_vs_ex04": value,
            }
        )
    return pd.DataFrame(rows)


@pytest.mark.parametrize(
    ("entry_share", "exit_share", "entry_improvements", "exit_improvements", "expected"),
    [
        (0.8, 0.2, [0.02, 0.02, -0.01], [0.0, 0.0, 0.0], "entry_failure"),
        (0.2, 0.8, [0.0, 0.0, 0.0], [0.02, 0.02, -0.01], "exit_failure"),
        (0.5, 0.5, [0.02, 0.02, 0.02], [0.02, 0.02, 0.02], "joint_entry_exit_failure"),
        (0.5, 0.5, [0.0, 0.0, 0.0], [0.0, 0.0, 0.0], "mixed_path_failure"),
    ],
)
def test_mechanism_classification_requires_path_and_hybrid_support(
    entry_share: float,
    exit_share: float,
    entry_improvements: list[float],
    exit_improvements: list[float],
    expected: str,
) -> None:
    """Catch a large path share being declared causal without counterfactual support."""
    result = classify_mechanism(
        _mechanism_rows(entry_share, exit_share),
        _hybrid_rows(entry_improvements, exit_improvements),
        _classification_protocol(),
    )

    assert result["classification"] == expected


def test_mechanism_classification_rejects_unclassified_path() -> None:
    """Catch an incomplete opportunity-loss ledger producing a confident mechanism."""
    rows = _mechanism_rows(0.8, 0.2)
    rows.loc[len(rows)] = {
        "window": "2021H1",
        "mechanism": "unclassified_baseline_only",
        "negative_log_loss": 0.1,
    }

    result = classify_mechanism(
        rows,
        _hybrid_rows([0.02, 0.02, 0.02], [0.0, 0.0, 0.0]),
        _classification_protocol(),
    )

    assert result["classification"] == "insufficient_path_evidence"


def test_mechanism_classification_uses_only_baseline_only_opportunity_loss() -> None:
    """Catch both-long losses diluting the preregistered entry/exit shares."""
    rows = _mechanism_rows(0.8, 0.2)
    rows.loc[len(rows)] = {
        "window": "2021H1",
        "mechanism": "both_long",
        "negative_log_loss": 100.0,
    }

    result = classify_mechanism(
        rows,
        _hybrid_rows([0.02, 0.02, 0.02], [0.0, 0.0, 0.0]),
        _classification_protocol(),
    )

    assert result["classification"] == "entry_failure"
    assert result["total_negative_log_loss"] == pytest.approx(3.0)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("holdout_access_allowed", True, "holdout"),
        ("visible_sample_end", "2026-01-01", "2025-12-31"),
    ],
)
def test_protocol_rejects_boundary_drift(key: str, value: object, message: str) -> None:
    """Catch the diagnosis being widened after preregistration."""
    protocol = copy.deepcopy(_classification_protocol())
    protocol[key] = value

    with pytest.raises(ValueError, match=message):
        validate_protocol(protocol)


def test_artifact_identity_rejects_changed_ex04_bytes(tmp_path: Path) -> None:
    """Catch a frozen research object changing after preregistration."""
    frozen = tmp_path / "frozen.json"
    frozen.write_bytes(b'{"version": 1}\n')

    with pytest.raises(ValueError, match="EX04.*hash"):
        validate_artifact_identity(frozen, "0" * 64, label="EX04")


def test_source_evidence_requires_every_declared_file_hash(tmp_path: Path) -> None:
    """Catch attribution silently reading regenerated EX02 evidence."""
    evidence = tmp_path / "evidence.csv"
    evidence.write_bytes(b"window,value\n2021H1,1\n")
    source = {
        "window_comparison_path": "evidence.csv",
        "window_comparison_sha256": "0" * 64,
    }

    with pytest.raises(ValueError, match="source evidence.*hash"):
        validate_source_evidence(tmp_path, source)


def test_visible_hashes_reject_any_2026_filename() -> None:
    """Catch a cutoff loader leaking holdout filenames into a formal diagnosis."""
    with pytest.raises(AssertionError, match="2026"):
        reject_holdout_hashes({"588080_daily_2026.csv": "abc"})


def test_formal_runner_validates_protocol_before_touching_inputs(tmp_path: Path) -> None:
    """Catch orchestration opening data before enforcing the no-holdout boundary."""
    protocol = copy.deepcopy(_classification_protocol())
    protocol["holdout_access_allowed"] = True

    with pytest.raises(ValueError, match="holdout"):
        run_ex04_path_attribution(tmp_path, tmp_path, tmp_path, protocol)


def test_path_ledger_rejects_window_closure_drift() -> None:
    """Catch daily mechanism rows that do not reconcile to terminal wealth."""
    ledger = pd.DataFrame(
        {
            "window": ["2021H1", "2021H1"],
            "log_wealth_delta": [0.01, -0.02],
            "path_mechanism": ["both_long", "late_entry"],
        }
    )
    metrics = pd.DataFrame(
        {
            "window": ["2021H1", "2021H1"],
            "variant": ["baseline", "ex04"],
            "strategy_return": [0.0, 0.0],
        }
    )

    with pytest.raises(AssertionError, match="2021H1.*close"):
        validate_path_ledger(ledger, metrics, ("2021H1",), tolerance=1e-12)


def test_path_ledger_rejects_unclassified_or_nonfinite_values() -> None:
    """Catch incomplete path labels and NaN values being archived as evidence."""
    ledger = pd.DataFrame(
        {
            "window": ["2021H1"],
            "log_wealth_delta": [float("nan")],
            "path_mechanism": ["unclassified_baseline_only"],
        }
    )
    metrics = pd.DataFrame(
        {
            "window": ["2021H1", "2021H1"],
            "variant": ["baseline", "ex04"],
            "strategy_return": [0.0, 0.0],
        }
    )

    with pytest.raises(ValueError, match="non-finite|unclassified"):
        validate_path_ledger(ledger, metrics, ("2021H1",), tolerance=1e-12)
