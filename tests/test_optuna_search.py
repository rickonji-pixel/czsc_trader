from __future__ import annotations

import pandas as pd
import pytest

from czsc_trader.optuna_search import (
    project_trial_parameters,
    rank_trial_results,
    validate_optuna_protocol,
)


NAMES = (
    "raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_10",
    "raw__daily__tas_macd_base_V221028__di_1__fastperiod_12__signalperiod_9__slowperiod_26",
    "raw__daily__vol_window_V230731__di_1__m_30__n_10__w_5",
    "state__alpha::up",
    "state__beta::down",
    "state__gamma::event",
    "state__delta::event",
    "state__epsilon::event",
)


def _protocol(**overrides: object) -> dict[str, object]:
    protocol: dict[str, object] = {
        "experiment_type": "optuna_joint_strategy_search",
        "status": "PRE_REGISTERED",
        "selection_sample_end": "2025-12-31",
        "selection_metric": "minimum_return_delta_vs_ex04",
        "holdout_access_before_freeze": False,
        "active_factor_count": {"low": 6, "high": 18},
        "raw_weight_bounds": [-1.0, 1.0],
        "minimum_absolute_weight": 0.0125,
        "minimum_trend_weight": 0.1,
        "protect_volume_window": True,
        "enter_threshold_bounds": [0.05, 0.3],
        "exit_gap_bounds": [0.025, 0.25],
        "optuna": {
            "version": "4.9.0",
            "sampler": "TPESampler",
            "seed": 20260824,
            "n_startup_trials": 256,
            "multivariate": True,
            "group": False,
            "constant_liar": True,
            "maximum_completed_trials": 4096,
            "minimum_completed_trials": 1024,
            "no_improvement_trials": 1024,
            "maximum_wall_time_seconds": 7200,
            "batch_size": 8,
        },
        "parallel": {
            "backend": "loky",
            "n_jobs": 8,
            "inner_max_num_threads": 1,
            "database_writer": "parent_only",
            "result_order": "trial_number",
        },
    }
    protocol.update(overrides)
    return protocol


def _origin() -> pd.Series:
    return pd.Series(
        [0.2, 0.15, 0.25, 0.1, 0.1, 0.1, 0.05, 0.05],
        index=NAMES,
        dtype=float,
    )


def test_projection_is_sparse_normalized_and_protected() -> None:
    raw = pd.Series(
        [0.01, -0.02, 0.001, 1.0, -0.9, 0.8, -0.7, 0.6],
        index=NAMES,
        dtype=float,
    )

    strategy = project_trial_parameters(
        NAMES, raw, 6, 0.18, 0.12, _origin(), _protocol()
    )

    active = strategy.weights[strategy.weights.ne(0.0)]
    trend_names = [name for name in NAMES if "tas_ma_" in name or "tas_macd_" in name]
    assert len(active) == 6
    assert float(active.abs().sum()) == pytest.approx(1.0)
    assert float(active.abs().min()) >= 0.0125
    assert float(strategy.weights.filter(like="vol_window_V230731").abs().sum()) > 0.0
    assert float(strategy.weights.loc[trend_names].abs().sum()) >= 0.1
    assert strategy.enter == pytest.approx(0.18)
    assert strategy.exit == pytest.approx(0.06)


def test_projection_ties_are_resolved_by_factor_name() -> None:
    raw = pd.Series(0.5, index=NAMES, dtype=float)

    strategy = project_trial_parameters(
        NAMES, raw, 6, 0.18, 0.12, _origin(), _protocol()
    )

    assert set(strategy.weights[strategy.weights.ne(0.0)].index) == {
        NAMES[0],
        NAMES[1],
        NAMES[2],
        "state__alpha::up",
        "state__beta::down",
        "state__delta::event",
    }


def test_projection_rejects_out_of_bounds_strategy_parameters() -> None:
    raw = pd.Series(0.5, index=NAMES, dtype=float)

    with pytest.raises(ValueError, match="active factor count"):
        project_trial_parameters(NAMES, raw, 5, 0.18, 0.12, _origin(), _protocol())
    with pytest.raises(ValueError, match="entry threshold"):
        project_trial_parameters(NAMES, raw, 6, 0.31, 0.12, _origin(), _protocol())
    with pytest.raises(ValueError, match="exit gap"):
        project_trial_parameters(NAMES, raw, 6, 0.18, 0.01, _origin(), _protocol())


def test_protocol_validation_rejects_search_boundary_changes() -> None:
    validate_optuna_protocol(_protocol())
    changed = _protocol(selection_metric="mean_return")

    with pytest.raises(ValueError, match="selection metric"):
        validate_optuna_protocol(changed)


def test_trial_ranking_prioritizes_worst_window_then_robust_tiebreaks() -> None:
    rows = pd.DataFrame(
        [
            {"trial_number": 3, "min_return_delta": 0.01, "win_count": 8, "median_return_delta": 0.02, "mean_return_delta": 0.03, "active_factor_count": 18},
            {"trial_number": 2, "min_return_delta": 0.02, "win_count": 7, "median_return_delta": 0.01, "mean_return_delta": 0.02, "active_factor_count": 12},
            {"trial_number": 1, "min_return_delta": 0.02, "win_count": 8, "median_return_delta": 0.01, "mean_return_delta": 0.02, "active_factor_count": 10},
        ]
    )

    ranked = rank_trial_results(rows)

    assert ranked["trial_number"].tolist() == [1, 2, 3]
