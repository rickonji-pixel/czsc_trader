from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "experiments" / "0902_EX03" / "run_experiment.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("range_weight_research", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_classify_trade_cycles_labels_regimes_and_short_losses() -> None:
    runner = _load_runner()
    sessions = pd.date_range("2025-01-02", periods=8, freq="B")
    regimes = pd.Series(
        ["range", "range", "range", "range", "trend", "trend", "trend", "trend"],
        index=sessions,
    )
    orders = pd.DataFrame(
        [
            {
                "signal_date": sessions[0],
                "execution_date": sessions[1],
                "side": "Buy",
                "size": 100.0,
                "price": 10.0,
                "fees": 1.0,
            },
            {
                "signal_date": sessions[2],
                "execution_date": sessions[3],
                "side": "Sell",
                "size": 100.0,
                "price": 9.0,
                "fees": 1.0,
            },
            {
                "signal_date": sessions[3],
                "execution_date": sessions[4],
                "side": "Buy",
                "size": 100.0,
                "price": 8.0,
                "fees": 1.0,
            },
            {
                "signal_date": sessions[5],
                "execution_date": sessions[6],
                "side": "Sell",
                "size": 100.0,
                "price": 10.0,
                "fees": 1.0,
            },
        ]
    )

    cycles = runner.classify_trade_cycles(orders, regimes, sessions)

    assert cycles["regime_path"].tolist() == ["range_to_range", "range_to_trend"]
    assert cycles["holding_sessions"].tolist() == [2, 2]
    assert cycles["short_loss"].tolist() == [True, False]
    np.testing.assert_allclose(
        cycles.loc[0, "net_return"], 899.0 / 1001.0 - 1.0, rtol=0.0, atol=1e-12
    )


def test_trade_diagnostics_compounds_each_regime_path() -> None:
    runner = _load_runner()
    cycles = pd.DataFrame(
        {
            "regime_path": ["range_to_range", "range_to_range", "range_to_trend"],
            "net_return": [-0.10, 0.20, 0.30],
            "short_loss": [True, False, False],
            "holding_sessions": [3, 12, 20],
            "fees": [10.0, 20.0, 30.0],
        }
    )

    summary = runner.trade_diagnostics(cycles)

    assert summary["range_to_range_count"] == 2
    assert summary["range_to_range_loss_count"] == 1
    assert summary["range_to_range_short_loss_count"] == 1
    np.testing.assert_allclose(
        summary["range_to_range_compound_return"], 0.08, rtol=0.0, atol=1e-12
    )
    np.testing.assert_allclose(
        summary["range_to_trend_compound_return"], 0.30, rtol=0.0, atol=1e-12
    )


def test_select_representatives_uses_preregistered_tie_breaks() -> None:
    runner = _load_runner()
    metrics = pd.DataFrame(
        {
            "candidate_id": [0, 1, 2, 3],
            "range_to_range_compound_return": [0.10, 0.30, 0.20, 0.20],
            "range_to_range_short_loss_count": [0, 2, 0, 0],
            "balanced_score": [0.20, 0.40, 0.90, 0.90],
            "weight_l1_distance": [0.10, 0.20, 0.30, 0.20],
        }
    )

    selected = runner.select_representatives(metrics)

    assert selected == {
        "best_pure_range_return": 1,
        "fewest_short_losses": 0,
        "best_balanced": 3,
    }


def test_cross_sample_consistency_reports_candidates_without_promoting_them() -> None:
    runner = _load_runner()
    research = pd.DataFrame(
        {
            "candidate_id": [0, 1, 18],
            "range_to_range_compound_return": [-0.10, -0.15, -0.20],
            "range_to_range_short_loss_count": [2, 4, 3],
        }
    )
    test = pd.DataFrame(
        {
            "candidate_id": [0, 1, 18],
            "range_to_range_compound_return": [0.10, 0.05, 0.08],
            "range_to_range_short_loss_count": [0, 2, 1],
        }
    )

    summary = runner.cross_sample_consistency(research, test, control_candidate_id=18)

    assert summary == {
        "research_pure_return_improved_count": 2,
        "test_pure_return_improved_count": 1,
        "both_samples_pure_return_improved_candidate_ids": [0],
        "both_samples_pure_return_and_short_loss_improved_candidate_ids": [0],
        "diagnostic_only": True,
        "promotion_allowed": False,
    }
