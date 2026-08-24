from datetime import date
import json
from pathlib import Path

import pytest

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
