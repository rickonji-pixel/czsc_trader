from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "experiments" / "0903_EX02" / "run_experiment.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("range_optimization", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shortlist_keeps_control_without_using_return() -> None:
    runner = _load_runner()
    profiles = pd.DataFrame(
        {
            "candidate_id": [0, 1, 2, 3],
            "first_front_count": [0, 3, 2, 1],
            "mean_pareto_layer": [9.0, 1.0, 1.5, 2.0],
            "worst_pareto_layer": [10, 1, 2, 3],
            "strategy_return": [9.0, -0.5, 0.1, 0.2],
        }
    )

    assert runner.shortlist_candidate_ids(profiles, control_id=0, limit=3) == [1, 2, 0]


def test_nearest_neighborhood_uses_factor_l1_distance() -> None:
    runner = _load_runner()
    candidates = pd.DataFrame(
        {
            "candidate_id": [10, 11, 12, 13],
            "f1": [0.50, 0.45, 0.20, 0.49],
            "f2": [0.30, 0.35, 0.30, 0.31],
            "f3": [0.20, 0.20, 0.50, 0.20],
        }
    )

    neighborhood = runner.nearest_neighborhood(
        candidates,
        representative_id=10,
        factor_names=("f1", "f2", "f3"),
        member_count=3,
    )

    assert neighborhood["candidate_id"].tolist() == [10, 13, 11]
    np.testing.assert_allclose(
        neighborhood["weight_l1_distance"], [0.0, 0.02, 0.1], rtol=0.0, atol=1e-12
    )


def test_execution_limit_family_maps_frozen_business_identity() -> None:
    runner = _load_runner()

    execution = SimpleNamespace(entry_limit_family="previous_close_ratio")

    assert runner.execution_limit_family(execution) == "fixed"
