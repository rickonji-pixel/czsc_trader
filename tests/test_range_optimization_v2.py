import copy
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_experiment():
    path = ROOT / "experiments" / "0903_EX04" / "run_experiment.py"
    spec = importlib.util.spec_from_file_location("ex04", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_candidate_payload_changes_only_range_research_identity():
    module = load_experiment()
    source = {
        "legacy_identity": {"version": "old"},
        "research_source": {"experiment_id": "old"},
        "rule": {
            "weights": {"range": {"a": 0.4, "b": 0.6}, "trend": {"a": 0.7, "b": 0.3}},
            "execution": {"fee": 1}, "candidate_id": 143, "experiment": "old", "research_end": "2025-12-31",
        },
    }
    original = copy.deepcopy(source)
    result = module.candidate_payload(source, "R0001", {"a": 0.2, "b": 0.8})
    assert source == original
    assert result["rule"]["weights"]["range"] == {"a": 0.2, "b": 0.8}
    assert result["rule"]["weights"]["trend"] == source["rule"]["weights"]["trend"]
    assert result["rule"]["execution"] == source["rule"]["execution"]
    assert "legacy_identity" not in result
    assert result["research_source"]["experiment_id"] == "0903_EX04"
