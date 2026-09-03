import csv
import json

from strategy_evaluator import MetricObservation, MetricStatus

from czsc_trader.evaluation_artifacts import EvaluationIdentity, load_reusable_observations
from czsc_trader.experiment_archive import build_experiment_manifest
from czsc_trader.identity import canonical_json_sha256


def _identity(**changes):
    values = {
        "candidate_id": "c1",
        "candidate_hash": "c" * 64,
        "execution_policy_hash": "e" * 64,
        "data_identity": "d" * 64,
        "development_cutoff": "2026-09-02",
        "window_id": "full",
        "window_start": "2021-01-04",
        "window_end": "2026-09-02",
        "tier": "SCREENING",
        "scenario_id": "standard",
        "fee_rate": 0.0005,
        "metric_semantics_version": "candidate-metrics-v1",
    }
    values.update(changes)
    return EvaluationIdentity(**values)


def _write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _write_completed_source(root):
    experiment = root / "experiments" / "0903_SOURCE"
    artifacts = experiment / "artifacts"
    artifacts.mkdir(parents=True)
    for name in ("01_goal.md", "02_design.md", "03_execution.md", "04_conclusion.md"):
        (experiment / name).write_text(f"# {name}\n", encoding="utf-8")
    source_files = {"daily": {"sha256": "source-data"}}
    protocol = {
        "development_cutoff": "2026-09-02",
        "candidate_manifest": "candidate_manifest.json",
    }
    manifest = {
        "source_files": source_files,
        "fee_rate": 0.0005,
        "metric_semantics_version": "candidate-metrics-v1",
        "windows": {"full": {"start": "2021-01-04", "end": "2026-09-02"}},
        "candidates": [{
            "candidate_id": "c1",
            "strategy_hash": "c" * 64,
            "execution_policy_hash": "e" * 64,
        }],
    }
    (experiment / "evaluation_protocol.json").write_text(json.dumps(protocol), encoding="utf-8")
    (experiment / "candidate_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    observation = MetricObservation(
        "c1", "full", "standard", "SCREENING", 0.1, 0.5, -0.2,
        0.5, MetricStatus.VALID, 2.0, MetricStatus.VALID, 20, 1.0, 0.01,
        (("full_return", 0.5),),
    )
    _write_csv(artifacts / "screening_metrics.csv", [observation.to_dict()])
    (artifacts / "evaluation_result.json").write_text('{"decision":"KEEP_INCUMBENT"}\n', encoding="utf-8")
    build_experiment_manifest(experiment, {
        "experiment_id": "0903_SOURCE",
        "status": "COMPLETE",
    })
    return experiment, canonical_json_sha256(source_files), observation


def test_evaluation_identity_changes_when_execution_changes():
    left = _identity(execution_policy_hash="a" * 64)
    right = _identity(execution_policy_hash="b" * 64)
    assert left.key != right.key


def test_loader_returns_only_exact_verified_hits(tmp_path):
    source, data_identity, expected = _write_completed_source(tmp_path)
    requested = (
        _identity(data_identity=data_identity),
        _identity(candidate_id="c2", candidate_hash="f" * 64, data_identity=data_identity),
    )
    result = load_reusable_observations(tmp_path / "experiments", (source.name,), requested)
    assert [item.to_dict() for item in result.observations] == [expected.to_dict()]
    assert result.missing_keys == (requested[1].key,)
    assert result.ledger[0].status == "REUSED"
    assert result.ledger[0].source_experiment == "0903_SOURCE"
    assert result.diagnostics == ()


def test_loader_treats_tampered_declared_source_as_miss(tmp_path):
    source, data_identity, _ = _write_completed_source(tmp_path)
    requested = (_identity(data_identity=data_identity),)
    metrics = source / "artifacts" / "screening_metrics.csv"
    metrics.write_text(metrics.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
    result = load_reusable_observations(tmp_path / "experiments", (source.name,), requested)
    assert result.observations == ()
    assert result.missing_keys == (requested[0].key,)
    assert result.diagnostics and "0903_SOURCE" in result.diagnostics[0]
