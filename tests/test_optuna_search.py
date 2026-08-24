from __future__ import annotations

import pandas as pd
import pytest
import optuna

from czsc_trader.optuna_search import (
    TrialOutcome,
    TrialRequest,
    enqueue_initial_trial,
    export_study_trials,
    project_trial_parameters,
    rank_trial_results,
    recover_running_trials,
    run_study_batches,
    suggest_trial_parameters,
    trial_params_for_strategy,
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


def test_volume_protection_uses_raw_factor_not_derived_state() -> None:
    derived = "state__raw__daily__vol_window_V230731__di_1::高量"
    names = (*NAMES, derived)
    raw = pd.Series(
        [0.01, 0.02, 0.001, 0.9, 0.8, 0.7, 0.6, 0.5, 1.0],
        index=names,
        dtype=float,
    )
    origin = _origin().reindex(names).fillna(0.0)

    strategy = project_trial_parameters(
        names, raw, 6, 0.18, 0.12, origin, _protocol()
    )

    assert strategy.weights[NAMES[2]] != 0.0


def test_trend_protection_uses_raw_factor_not_derived_state() -> None:
    derived = "state__raw__daily__tas_ma_base_V221101__di_1::多头"
    names = (*NAMES, derived)
    raw = pd.Series(
        [0.001, 0.002, 0.8, 0.9, 0.7, 0.6, 0.5, 0.4, 1.0],
        index=names,
        dtype=float,
    )
    origin = _origin().reindex(names).fillna(0.0)

    strategy = project_trial_parameters(
        names, raw, 6, 0.18, 0.12, origin, _protocol()
    )

    raw_trend_mass = strategy.weights.loc[[NAMES[0], NAMES[1]]].abs().sum()
    assert float(raw_trend_mass) >= 0.1


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


def _study(seed: int = 7) -> optuna.Study:
    return optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=seed, n_startup_trials=2),
    )


def _request(trial: optuna.Trial) -> TrialRequest:
    value = trial.suggest_float("x", -1.0, 1.0)
    return TrialRequest(trial.number, {"x": value})


def _evaluate(requests: tuple[TrialRequest, ...]) -> tuple[TrialOutcome, ...]:
    return tuple(
        TrialOutcome(
            number=request.number,
            value=-abs(float(request.payload["x"]) - 0.25),
            user_attrs={"x_squared": float(request.payload["x"]) ** 2},
        )
        for request in requests
    )


def test_batch_tell_order_does_not_depend_on_worker_completion_order() -> None:
    forward = _study()
    reverse = _study()

    run_study_batches(
        forward,
        _request,
        _evaluate,
        maximum_completed_trials=8,
        minimum_completed_trials=8,
        no_improvement_trials=8,
        maximum_wall_time_seconds=60,
        batch_size=4,
    )
    run_study_batches(
        reverse,
        _request,
        lambda requests: tuple(reversed(_evaluate(requests))),
        maximum_completed_trials=8,
        minimum_completed_trials=8,
        no_improvement_trials=8,
        maximum_wall_time_seconds=60,
        batch_size=4,
    )

    assert [trial.params for trial in forward.trials] == [trial.params for trial in reverse.trials]
    assert [trial.value for trial in forward.trials] == [trial.value for trial in reverse.trials]
    assert forward.best_params == reverse.best_params


def test_missing_or_duplicate_batch_results_abort_the_study() -> None:
    with pytest.raises(RuntimeError, match="trial result numbers"):
        run_study_batches(
            _study(),
            _request,
            lambda requests: (TrialOutcome(requests[0].number, 1.0, {}),) * 2,
            maximum_completed_trials=2,
            minimum_completed_trials=2,
            no_improvement_trials=2,
            maximum_wall_time_seconds=60,
            batch_size=2,
        )


def test_running_trials_are_failed_before_resume() -> None:
    study = _study()
    trial = study.ask()
    trial.suggest_float("x", -1.0, 1.0)

    recovered = recover_running_trials(study)

    assert recovered == (0,)
    assert study.trials[0].state == optuna.trial.TrialState.FAIL


def test_initial_trial_is_enqueued_only_for_an_empty_study() -> None:
    study = _study()

    assert enqueue_initial_trial(study, {"x": 0.25})
    first = study.ask()
    assert first.suggest_float("x", -1.0, 1.0) == pytest.approx(0.25)
    study.tell(first, 0.0)
    assert not enqueue_initial_trial(study, {"x": 0.75})


def test_trial_export_contains_params_attributes_and_state(tmp_path) -> None:
    study = _study()
    run_study_batches(
        study,
        _request,
        _evaluate,
        maximum_completed_trials=2,
        minimum_completed_trials=2,
        no_improvement_trials=2,
        maximum_wall_time_seconds=60,
        batch_size=2,
    )
    path = tmp_path / "trials.csv"

    exported = export_study_trials(study, path)

    assert path.is_file()
    assert exported["number"].tolist() == [0, 1]
    assert exported["state"].tolist() == ["COMPLETE", "COMPLETE"]
    assert "param::x" in exported
    assert "attr::x_squared" in exported


def test_frozen_strategy_round_trips_through_trial_parameters() -> None:
    desired = project_trial_parameters(
        NAMES,
        pd.Series([0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1], index=NAMES),
        6,
        0.175,
        0.15,
        _origin(),
        _protocol(),
    )
    params = trial_params_for_strategy(
        desired.weights, desired.enter, desired.exit, _protocol()
    )

    replayed = suggest_trial_parameters(
        optuna.trial.FixedTrial(params), NAMES, _origin(), _protocol()
    )

    pd.testing.assert_series_equal(replayed.weights, desired.weights, atol=1e-12, rtol=0.0)
    assert replayed.enter == pytest.approx(desired.enter)
    assert replayed.exit == pytest.approx(desired.exit)
