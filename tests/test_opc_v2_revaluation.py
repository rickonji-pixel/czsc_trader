import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_experiment():
    path = ROOT / "experiments" / "0903_EX05" / "run_experiment.py"
    spec = importlib.util.spec_from_file_location("ex05", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_benchmark():
    path = ROOT / "experiments" / "0903_EX05" / "benchmark_evaluation.py"
    spec = importlib.util.spec_from_file_location("ex05_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_revaluation_preserves_candidate_identity_and_relabels_trials():
    module = load_experiment()
    source = {
        "schema_version": 1,
        "experiment_id": "0903_EX04",
        "prepared_from_commit": "old",
        "candidates": [{"candidate_id": "S001-v1", "strategy_hash": "a"}],
        "trials": [{"trial_id": "EX04-S001-v1", "candidate_id": "S001-v1"}],
        "audits": {"candidate_count": 1},
    }
    result = module.candidate_manifest_from_source(source)
    assert result["experiment_id"] == "0903_EX05"
    assert result["candidates"] == source["candidates"]
    assert result["trials"][0]["trial_id"] == "EX05-S001-v1"
    assert result["source_experiment"]["experiment_id"] == "0903_EX04"
    assert result["prepared_from_commit"] == "9e7037ca5e6abd35f22a8368178541d4d423900b"
    assert result["evaluation_workers"] in {2, 4, 8}
    assert result["reuse_experiment_artifacts"] is True
    assert result["reuse_source_experiments"] == ["0903_EX04"]
    assert result["metric_semantics_version"] == "candidate-metrics-v1"


def test_benchmark_candidate_selection_spans_sorted_pool():
    module = load_benchmark()
    candidates = [{"candidate_id": f"R{index:04d}"} for index in range(100)]
    candidates.insert(0, {"candidate_id": "S001-v1", "is_incumbent": True})
    selected = module.representative_ids(candidates, count=4)
    assert selected == ("R0000", "R0033", "R0066", "R0099")


def test_full_scale_source_hash_matches_recorded_cold_run():
    module = load_benchmark()
    benchmark = json.loads(
        (ROOT / "experiments" / "0903_EX05" / "artifacts" / "evaluation_benchmark.json").read_text(
            encoding="utf-8"
        )
    )
    source_count, source_hash = module.source_screening_metric_hash()
    full_scale = benchmark["full_scale_cold"]
    assert source_count == full_scale["observation_count"]
    assert source_hash == full_scale["metric_hash"]
