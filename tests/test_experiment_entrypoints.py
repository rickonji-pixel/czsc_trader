from datetime import date
import json
from pathlib import Path
import sys

import pytest
import pandas as pd

from czsc_trader.experiment_archive import (
    REQUIRED_DOCUMENTS,
    build_experiment_manifest,
    validate_experiment_archive,
)
import scripts.run_experiment as entrypoint
import scripts.run_holdout as holdout_entrypoint


def test_pre2026_entrypoint_writes_a_complete_experiment_archive(
    tmp_path: Path, monkeypatch
) -> None:
    received: dict[str, Path] = {}
    protocol_path = tmp_path / "protocol.json"
    protocol_path.write_text(
        json.dumps(
            {
                "experiment_id": "588080_reentry_v1",
                "symbol": "588080.SH",
                "champion": "baseline_20260823",
                "visible_sample_end": "2025-12-31",
                "pass_rule": "strict return and Sharpe in every window",
            }
        ),
        encoding="utf-8",
    )

    def fake_runner(raw_dir: Path, baseline_root: Path, artifacts: Path):
        received.update(raw_dir=raw_dir, baseline_root=baseline_root, artifacts=artifacts)
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "candidate_results.csv").write_text(
            "candidate_id,pass_count\ncooldown0_gate-trend,1\n", encoding="utf-8"
        )
        (artifacts / "champion_metrics.json").write_text("{}\n", encoding="utf-8")
        (artifacts / "metrics.json").write_text(
            json.dumps(
                {
                    "status": "FAIL",
                    "best_candidate": "cooldown0_gate-trend",
                    "best_pass_count": 1,
                    "sample_cutoff": "2025-12-31",
                    "visible_data_hashes": {"588080_daily_2025.csv": "abc"},
                    "audit": None,
                }
            ),
            encoding="utf-8",
        )
        return {
            "status": "FAIL",
            "best_candidate": "cooldown0_gate-trend",
            "best_pass_count": 1,
            "frozen_challenger": None,
            "output_dir": str(artifacts),
        }

    monkeypatch.setattr(entrypoint, "run_pre2026_experiment", fake_runner)

    archive = entrypoint.main(
        tmp_path,
        run_date=date(2026, 8, 24),
        protocol_path=protocol_path,
    )

    assert archive == tmp_path / "0824_EX01"
    assert received["artifacts"] == archive / "artifacts"
    assert (archive / "artifacts" / "protocol.json").is_file()
    assert all((archive / name).is_file() for name in REQUIRED_DOCUMENTS)
    manifest = validate_experiment_archive(archive)
    assert manifest["status"] == "FAIL"
    assert manifest["holdout_accessed"] is False
    assert "outputs/" not in json.dumps(manifest)


def test_pre2026_entrypoint_preserves_failed_round_as_error_archive(
    tmp_path: Path, monkeypatch
) -> None:
    protocol_path = tmp_path / "protocol.json"
    protocol_path.write_text(
        json.dumps(
            {
                "experiment_id": "broken",
                "symbol": "588080.SH",
                "champion": "baseline_20260823",
                "visible_sample_end": "2025-12-31",
                "pass_rule": "strict",
            }
        ),
        encoding="utf-8",
    )

    def failing_runner(*args, **kwargs):
        raise RuntimeError("simulated research failure")

    monkeypatch.setattr(entrypoint, "run_pre2026_experiment", failing_runner)

    with pytest.raises(RuntimeError, match="simulated research failure"):
        entrypoint.main(
            tmp_path,
            run_date=date(2026, 8, 24),
            protocol_path=protocol_path,
        )

    archive = tmp_path / "0824_EX01"
    manifest = validate_experiment_archive(archive)
    assert manifest["status"] == "ERROR"
    assert "simulated research failure" in (archive / "03_execution.md").read_text(
        encoding="utf-8"
    )


def test_holdout_entrypoint_creates_a_separate_experiment_archive(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "0824_EX01"
    (source / "artifacts").mkdir(parents=True)
    for name in REQUIRED_DOCUMENTS:
        (source / name).write_text(f"# {name}\n", encoding="utf-8")
    frozen = source / "artifacts" / "frozen_challenger.json"
    frozen.write_text(
        json.dumps(
            {
                "sample_end": "2025-12-31",
                "champion": {"version": "baseline_20260823", "sha256": "abc"},
                "challenger": {"cooldown_days": 2, "reentry_gate": "trend"},
            }
        ),
        encoding="utf-8",
    )
    build_experiment_manifest(
        source,
        {
            "experiment_id": source.name,
            "date": "2026-08-24",
            "status": "PASS",
            "symbol": "588080.SH",
            "asset_type": "etf",
            "visible_sample_end": "2025-12-31",
            "holdout_accessed": False,
        },
    )

    def fake_holdout(raw_dir, baseline_root, frozen_path, artifacts):
        artifacts.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": "FAIL",
            "champion": {"version": "baseline_20260823", "sha256": "abc"},
            "challenger": {"cooldown_days": 2, "reentry_gate": "trend"},
            "windows": {},
            "audit": {"status": "PASS"},
            "holdout_cutoff": "2026-08-21",
            "data_hashes": {"588080_daily_2026.csv": "abc"},
            "output_dir": str(artifacts),
        }
        (artifacts / "metrics.json").write_text(json.dumps(payload), encoding="utf-8")
        (artifacts / "orders.csv").write_text("\n", encoding="utf-8")
        return payload

    monkeypatch.setattr(holdout_entrypoint, "run_2026_holdout", fake_holdout)

    archive = holdout_entrypoint.main(
        frozen,
        experiments_root=tmp_path,
        run_date=date(2026, 8, 24),
    )

    assert archive == tmp_path / "0824_EX02"
    manifest = validate_experiment_archive(archive)
    assert manifest["holdout_accessed"] is True
    assert manifest["status"] == "FAIL"


def _preregistered_attribution_archive(tmp_path: Path) -> Path:
    archive = tmp_path / "0824_EX02"
    (archive / "artifacts").mkdir(parents=True)
    for name in REQUIRED_DOCUMENTS:
        (archive / name).write_text(f"# {name}\n", encoding="utf-8")
    (archive / "artifacts" / "protocol.json").write_text(
        json.dumps(
            {
                "experiment_id": "0824_EX02",
                "experiment_type": "champion_attribution",
                "status": "PRE_REGISTERED",
                "symbol": "588080.SH",
                "visible_sample_end": "2025-12-31",
                "holdout_access_allowed": False,
                "champion": {
                    "version": "baseline_20260823",
                    "sha256": "abc123",
                },
                "promotion": {
                    "select_challenger": False,
                    "write_frozen_challenger": False,
                    "update_champion": False,
                    "force_negative_factor": False,
                },
            }
        ),
        encoding="utf-8",
    )
    return archive


def test_preregistered_attribution_finalizes_same_archive(
    tmp_path: Path, monkeypatch
) -> None:
    """Catch attribution allocating a new experiment or enabling promotion."""
    archive = _preregistered_attribution_archive(tmp_path)

    def fake_attribution(raw_dir, baseline_root, artifacts_dir, protocol):
        assert artifacts_dir == archive / "artifacts"
        assert protocol["holdout_access_allowed"] is False
        (artifacts_dir / "classification.csv").write_text(
            "object_type,object_id,counterfactual,classification,median_return_delta,median_sharpe_delta\n"
            "group,trend,zero_contribution,stable_negative,0.01,0.1\n",
            encoding="utf-8",
        )
        (artifacts_dir / "metrics.json").write_text("{}\n", encoding="utf-8")
        return {
            "status": "COMPLETE",
            "stable_negative": [
                {
                    "object_type": "group",
                    "object_id": "trend",
                    "counterfactual": "zero_contribution",
                }
            ],
            "classification_counts": {"stable_negative": 1},
            "visible_data_hashes": {"588080_daily_2025.csv": "abc"},
            "holdout_accessed": False,
            "frozen_challenger": None,
        }

    monkeypatch.setattr(entrypoint, "run_champion_attribution", fake_attribution)

    finalized = entrypoint.run_preregistered_attribution(archive)

    assert finalized == archive
    manifest = validate_experiment_archive(archive)
    assert manifest["status"] == "COMPLETE"
    assert manifest["holdout_accessed"] is False
    assert "trend" in (archive / "04_conclusion.md").read_text(encoding="utf-8")
    assert not (archive / "artifacts" / "frozen_challenger.json").exists()


def test_preregistered_attribution_rejects_any_promotion_flag(tmp_path: Path) -> None:
    """Catch a diagnostic protocol being silently converted into candidate selection."""
    archive = _preregistered_attribution_archive(tmp_path)
    path = archive / "artifacts" / "protocol.json"
    protocol = json.loads(path.read_text(encoding="utf-8"))
    protocol["promotion"]["select_challenger"] = True
    path.write_text(json.dumps(protocol), encoding="utf-8")

    with pytest.raises(ValueError, match="promotion"):
        entrypoint.run_preregistered_attribution(archive)


def _preregistered_ex04_attribution_archive(tmp_path: Path) -> Path:
    archive = tmp_path / "0825_EX02"
    (archive / "artifacts").mkdir(parents=True)
    for name in REQUIRED_DOCUMENTS:
        (archive / name).write_text(f"# {name}\n", encoding="utf-8")
    protocol = json.loads(
        Path("experiments/0825_EX02/artifacts/protocol.json").read_text(encoding="utf-8")
    )
    (archive / "artifacts" / "protocol.json").write_text(
        json.dumps(protocol), encoding="utf-8"
    )
    return archive


def test_preregistered_ex04_attribution_finalizes_same_archive(
    tmp_path: Path, monkeypatch
) -> None:
    """Catch EX04 diagnosis allocating another experiment or enabling a holdout."""
    archive = _preregistered_ex04_attribution_archive(tmp_path)

    def fake_runner(raw_dir, baseline_root, experiment_dir, protocol):
        assert experiment_dir == archive
        assert protocol["holdout_access_allowed"] is False
        artifacts = experiment_dir / "artifacts"
        pd.DataFrame(
            [
                {
                    "object_type": "component",
                    "object_id": "weights",
                    "classification": "stable_positive",
                    "positive_years": 4,
                    "negative_years": 0,
                    "median_return_contribution": 0.02,
                    "median_sharpe_contribution": 0.1,
                    "single_interval_dominated": False,
                }
            ]
        ).to_csv(artifacts / "classification.csv", index=False)
        pd.DataFrame(
            [
                {
                    "window": "2021",
                    "baseline_return": 0.1,
                    "ex04_return": 0.12,
                    "return_delta": 0.02,
                    "baseline_sharpe": 1.0,
                    "ex04_sharpe": 1.1,
                    "sharpe_delta": 0.1,
                    "baseline_max_drawdown": -0.1,
                    "ex04_max_drawdown": -0.08,
                    "baseline_exposure": 0.5,
                    "ex04_exposure": 0.45,
                    "baseline_trade_count": 4,
                    "ex04_trade_count": 4,
                }
            ]
        ).to_csv(artifacts / "window_comparison.csv", index=False)
        pd.DataFrame(
            [{"regime": "uptrend", "daily_return_delta_sum": 0.03}]
        ).to_csv(artifacts / "regime_attribution.csv", index=False)
        (artifacts / "local_geometry.json").write_text(
            json.dumps({"classification": "broad_plateau"}), encoding="utf-8"
        )
        (artifacts / "bootstrap_summary.json").write_text(
            json.dumps(
                {
                    "replications": 2000,
                    "quantiles": {"0.025": -0.01, "0.5": 0.02, "0.975": 0.05},
                    "probability_delta_above_zero": 0.8,
                }
            ),
            encoding="utf-8",
        )
        payload = {
            "status": "COMPLETE",
            "visible_data_hashes": {"588080_daily_2025.csv": "abc"},
            "holdout_accessed": False,
            "frozen_challenger": None,
            "component_variant_count": 4,
            "local_perturbation_count": 32,
            "group_coalition_count": 8,
            "single_interval_dominated": False,
        }
        (artifacts / "metrics.json").write_text(json.dumps(payload), encoding="utf-8")
        return payload

    monkeypatch.setattr(entrypoint, "run_ex04_attribution", fake_runner)

    finalized = entrypoint.run_preregistered_ex04_attribution(archive)

    assert finalized == archive
    manifest = validate_experiment_archive(archive)
    assert manifest["status"] == "COMPLETE"
    assert manifest["holdout_accessed"] is False
    conclusion = (archive / "04_conclusion.md").read_text(encoding="utf-8")
    assert "weights" in conclusion
    assert "broad_plateau" in conclusion
    assert not (archive / "artifacts" / "frozen_challenger.json").exists()


def test_preregistered_ex04_attribution_rejects_promotion(
    tmp_path: Path,
) -> None:
    """Catch a diagnostic archive being converted into same-round optimization."""
    archive = _preregistered_ex04_attribution_archive(tmp_path)
    path = archive / "artifacts" / "protocol.json"
    protocol = json.loads(path.read_text(encoding="utf-8"))
    protocol["promotion"]["optimize_parameters"] = True
    path.write_text(json.dumps(protocol), encoding="utf-8")

    with pytest.raises(ValueError, match="promotion"):
        entrypoint.run_preregistered_ex04_attribution(archive)


def _preregistered_ex04_path_archive(tmp_path: Path) -> Path:
    archive = tmp_path / "0825_EX03"
    (archive / "artifacts").mkdir(parents=True)
    for name in REQUIRED_DOCUMENTS:
        (archive / name).write_text(f"# {name}\n", encoding="utf-8")
    protocol = json.loads(
        Path("experiments/0825_EX03/artifacts/protocol.json").read_text(encoding="utf-8")
    )
    (archive / "artifacts" / "protocol.json").write_text(
        json.dumps(protocol), encoding="utf-8"
    )
    return archive


def test_preregistered_ex04_path_attribution_finalizes_same_archive(
    tmp_path: Path, monkeypatch
) -> None:
    """Catch EX03 allocating another archive, holdout, or frozen challenger."""
    archive = _preregistered_ex04_path_archive(tmp_path)

    def fake_runner(raw_dir, baseline_root, experiment_dir, protocol):
        assert experiment_dir == archive
        assert protocol["holdout_access_allowed"] is False
        artifacts = experiment_dir / "artifacts"
        classification = {
            "classification": "entry_failure",
            "total_negative_log_loss": 0.2,
            "unclassified_negative_log_loss": 0.0,
            "entry": {
                "pooled_negative_log_share": 0.8,
                "hybrid_improved_windows": 2,
                "hybrid_median_return_improvement": 0.01,
            },
            "exit": {
                "pooled_negative_log_share": 0.2,
                "hybrid_improved_windows": 0,
                "hybrid_median_return_improvement": 0.0,
            },
        }
        (artifacts / "mechanism_classification.json").write_text(
            json.dumps(classification), encoding="utf-8"
        )
        pd.DataFrame(
            [
                {
                    "window": "2021H1",
                    "mechanism": "late_entry",
                    "negative_log_loss": 0.2,
                }
            ]
        ).to_csv(artifacts / "mechanism_contributions.csv", index=False)
        payload = {
            "status": "COMPLETE",
            "visible_data_hashes": {"588080_daily_2025.csv": "abc"},
            "holdout_accessed": False,
            "frozen_challenger": None,
            "variant_count": 6,
            "window_count": 10,
            "baseline_episode_count": 12,
            "decision_event_count": 7,
            "max_absolute_closure_residual": 1e-16,
            "mechanism_classification": classification,
        }
        (artifacts / "metrics.json").write_text(json.dumps(payload), encoding="utf-8")
        return payload

    monkeypatch.setattr(entrypoint, "run_ex04_path_attribution", fake_runner)

    finalized = entrypoint.run_preregistered_ex04_path_attribution(archive)

    assert finalized == archive
    manifest = validate_experiment_archive(archive)
    assert manifest["status"] == "COMPLETE"
    assert manifest["holdout_accessed"] is False
    conclusion = (archive / "04_conclusion.md").read_text(encoding="utf-8")
    assert "entry_failure" in conclusion
    assert "不访问2026" in conclusion
    assert not (archive / "artifacts" / "frozen_challenger.json").exists()


def test_cli_dispatches_ex04_path_protocol_to_stable_entrypoint(
    tmp_path: Path, monkeypatch
) -> None:
    """Catch EX03 falling through to an unsupported or legacy experiment runner."""
    archive = _preregistered_ex04_path_archive(tmp_path)
    called: list[Path] = []

    def fake_entrypoint(experiment_dir: Path) -> Path:
        called.append(experiment_dir)
        return experiment_dir

    monkeypatch.setattr(entrypoint, "run_preregistered_ex04_path_attribution", fake_entrypoint)
    monkeypatch.setattr(
        sys, "argv", ["run_experiment.py", "--experiment-dir", str(archive)]
    )

    entrypoint.cli()

    assert called == [archive]


def _preregistered_exit_signal_archive(tmp_path: Path) -> Path:
    archive = tmp_path / "0825_EX04"
    (archive / "artifacts").mkdir(parents=True)
    for name in REQUIRED_DOCUMENTS:
        (archive / name).write_text(f"# {name}\n", encoding="utf-8")
    protocol = json.loads(
        Path("experiments/0825_EX04/artifacts/protocol.json").read_text(encoding="utf-8")
    )
    (archive / "artifacts" / "protocol.json").write_text(
        json.dumps(protocol), encoding="utf-8"
    )
    return archive


def test_preregistered_exit_signal_diagnosis_finalizes_same_archive(
    tmp_path: Path, monkeypatch
) -> None:
    """Catch EX04 allocating a new archive or turning diagnosis into a candidate."""
    archive = _preregistered_exit_signal_archive(tmp_path)

    def fake_runner(raw_dir, experiment_dir, protocol):
        assert experiment_dir == archive
        assert protocol["holdout_access_allowed"] is False
        classification = {
            "classification": "context_dependent_exit_quality",
            "event_count": 20,
            "false_exit_count": 8,
            "protective_exit_count": 6,
            "neutral_exit_count": 6,
            "concentration": {
                "total_false_exit_log_loss": 0.2,
                "top1_share": 0.2,
                "top3_share": 0.45,
                "concentrated": False,
            },
        }
        artifacts = experiment_dir / "artifacts"
        (artifacts / "exit_quality_classification.json").write_text(
            json.dumps(classification), encoding="utf-8"
        )
        payload = {
            "status": "COMPLETE",
            "visible_data_hashes": {"588080_daily_2025.csv": "abc"},
            "holdout_accessed": False,
            "frozen_challenger": None,
            "event_count": 20,
            "path_ledger_rows": 100,
            "endpoint_counts": {"baseline_exit_execution": 10, "ex04_reentry_execution": 10},
            "outcome_counts": {
                "false_exit": 8,
                "protective_exit": 6,
                "neutral_exit": 6,
            },
            "max_absolute_closure_residual": 1e-16,
            "qualifying_false_contexts": 1,
            "qualifying_protective_contexts": 1,
            "stable_large_effect_features": 0,
            "exit_quality_classification": classification,
        }
        (artifacts / "metrics.json").write_text(json.dumps(payload), encoding="utf-8")
        return payload

    monkeypatch.setattr(entrypoint, "run_exit_signal_diagnosis", fake_runner)

    finalized = entrypoint.run_preregistered_exit_signal_diagnosis(archive)

    assert finalized == archive
    manifest = validate_experiment_archive(archive)
    assert manifest["status"] == "COMPLETE"
    assert manifest["holdout_accessed"] is False
    conclusion = (archive / "04_conclusion.md").read_text(encoding="utf-8")
    assert "context_dependent_exit_quality" in conclusion
    assert "不能直接作为交易规则" in conclusion
    assert not (archive / "artifacts" / "frozen_challenger.json").exists()
    assert not (archive / "artifacts" / "orders.csv").exists()


def test_cli_dispatches_exit_signal_protocol_to_stable_entrypoint(
    tmp_path: Path, monkeypatch
) -> None:
    """Catch EX04 falling through to an optimizer or unsupported protocol path."""
    archive = _preregistered_exit_signal_archive(tmp_path)
    called: list[Path] = []

    def fake_entrypoint(experiment_dir: Path) -> Path:
        called.append(experiment_dir)
        return experiment_dir

    monkeypatch.setattr(entrypoint, "run_preregistered_exit_signal_diagnosis", fake_entrypoint)
    monkeypatch.setattr(
        sys, "argv", ["run_experiment.py", "--experiment-dir", str(archive)]
    )

    entrypoint.cli()

    assert called == [archive]


def _preregistered_dominant_anatomy_archive(tmp_path: Path) -> Path:
    archive = tmp_path / "0825_EX05"
    (archive / "artifacts").mkdir(parents=True)
    for name in REQUIRED_DOCUMENTS:
        (archive / name).write_text(f"# {name}\n", encoding="utf-8")
    protocol = json.loads(
        Path("experiments/0825_EX05/artifacts/protocol.json").read_text(encoding="utf-8")
    )
    (archive / "artifacts" / "protocol.json").write_text(
        json.dumps(protocol), encoding="utf-8"
    )
    return archive


def test_preregistered_dominant_anatomy_finalizes_same_archive(
    tmp_path: Path, monkeypatch
) -> None:
    """Catch EX05 allocating another archive or promoting a diagnostic signature."""
    archive = _preregistered_dominant_anatomy_archive(tmp_path)

    def fake_runner(raw_dir, experiment_dir, protocol):
        assert experiment_dir == archive
        assert protocol["research_baseline"]["experiment_id"] == "0824_EX04"
        assert protocol["source_archive"]["experiment_id"] == "0825_EX04"
        artifacts = experiment_dir / "artifacts"
        classification = {
            "classification": "shared_confirmed_low_contamination_anatomy",
            "event_count": 20,
            "discovered_signature_count": 4,
            "confirmed_signature_count": 2,
            "low_contamination_signature_count": 1,
        }
        (artifacts / "anatomy_classification.json").write_text(
            json.dumps(classification), encoding="utf-8"
        )
        pd.DataFrame(
            [
                {
                    "factor": "raw__daily__example",
                    "descriptor": "run_length_bin",
                    "value": "2_3",
                    "protective_support": 1,
                    "joint_margin_protective_support": 0,
                    "low_contamination": True,
                }
            ]
        ).to_csv(artifacts / "confirmed_atomic_signatures.csv", index=False)
        payload = {
            "status": "COMPLETE",
            "visible_data_hashes": {"588080_daily_2025.csv": "abc"},
            "holdout_accessed": False,
            "frozen_challenger": None,
            "event_count": 20,
            "factor_count": 12,
            "trajectory_rows": 420,
            "descriptor_count": 124,
            "discovered_signature_count": 4,
            "confirmed_signature_count": 2,
            "low_contamination_signature_count": 1,
            "max_signal_day_score_error": 1e-16,
            "anatomy_classification": classification,
        }
        (artifacts / "metrics.json").write_text(json.dumps(payload), encoding="utf-8")
        return payload

    monkeypatch.setattr(entrypoint, "run_dominant_exit_anatomy", fake_runner)

    finalized = entrypoint.run_preregistered_dominant_exit_anatomy(archive)

    assert finalized == archive
    manifest = validate_experiment_archive(archive)
    assert manifest["status"] == "COMPLETE"
    assert manifest["research_object"]["path"].startswith("experiments/0824_EX04/")
    execution = (archive / "03_execution.md").read_text(encoding="utf-8")
    assert "执行提交" in execution
    conclusion = (archive / "04_conclusion.md").read_text(encoding="utf-8")
    assert "shared_confirmed_low_contamination_anatomy" in conclusion
    assert "不是交易规则" in conclusion
    assert "0824_EX04" in conclusion and "0825_EX04" in conclusion
    assert not (archive / "artifacts" / "frozen_challenger.json").exists()
    assert not (archive / "artifacts" / "orders.csv").exists()


def test_cli_dispatches_dominant_anatomy_protocol_to_stable_entrypoint(
    tmp_path: Path, monkeypatch
) -> None:
    """Catch EX05 falling through to an optimizer or an older EX04 diagnostic."""
    archive = _preregistered_dominant_anatomy_archive(tmp_path)
    called: list[Path] = []

    def fake_entrypoint(experiment_dir: Path) -> Path:
        called.append(experiment_dir)
        return experiment_dir

    monkeypatch.setattr(entrypoint, "run_preregistered_dominant_exit_anatomy", fake_entrypoint)
    monkeypatch.setattr(
        sys, "argv", ["run_experiment.py", "--experiment-dir", str(archive)]
    )

    entrypoint.cli()

    assert called == [archive]
