from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "experiments" / "0903_EX03" / "run_experiment.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("range_optimization_correction", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deployable_candidates_apply_factor_floor_before_ranking() -> None:
    runner = _load_runner()
    candidates = pd.DataFrame(
        {
            "candidate_id": [1, 2, 3],
            "weight__f1": [0.50, 0.495, 0.60],
            "weight__f2": [0.495, 0.500, 0.399],
            "weight__f3": [0.005, 0.005, 0.001],
        }
    )

    selected = runner.deployable_candidates(
        candidates, factor_names=("f1", "f2", "f3"), minimum_weight=0.005
    )

    assert selected["candidate_id"].tolist() == [1, 2]
    assert selected["minimum_factor_weight"].tolist() == [0.005, 0.005]
