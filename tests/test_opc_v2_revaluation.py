import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_experiment():
    path = ROOT / "experiments" / "0903_EX05" / "run_experiment.py"
    spec = importlib.util.spec_from_file_location("ex05", path)
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
