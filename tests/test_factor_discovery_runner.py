from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest

from czsc_trader.factor_discovery import CandidateFactors
from czsc_trader.factor_discovery_runner import (
    RollingFitTask,
    RollingFitInputs,
    _benchmark_parallel_jobs,
    _fit_rolling_weights,
    _rolling_fit_tasks,
    _run_rolling_fit_task,
    _write_event_signal_support,
    build_comparison_window,
    build_factor_specs,
    factor_holdout_pass,
    training_windows_before,
    validate_factor_protocol,
)
from czsc_trader.return_only_runner import half_year_periods


def _protocol() -> dict[str, object]:
    return {
        "experiment_type": "event_aware_parallel_factor_discovery",
        "status": "PRE_REGISTERED",
        "selection_sample_end": "2025-12-31",
        "validation_windows": [
            "2022H1", "2022H2", "2023H1", "2023H2",
            "2024H1", "2024H2", "2025H1", "2025H2",
        ],
        "holdout_windows": ["2026Q1", "2026H1", "2026M1-M8"],
        "weight_steps": [0.025, 0.05],
        "coordinate_rounds": [1, 2],
        "enter_thresholds": [0.125, 0.15, 0.175, 0.2],
        "exit_thresholds": [-0.025, 0.0, 0.025, 0.05],
        "algorithm_config_count": 64,
        "selection_metric": "strategy_return_only",
        "holdout_access_before_freeze": False,
        "new_daily_signals": [f"signal_{index}" for index in range(12)] + ["cxt_first_sell_V221126"],
        "event_signal_requirements": [
            "cxt_first_buy_V221126",
            "cxt_first_sell_V221126",
            "cxt_second_buy_V230320",
            "cxt_second_sell_V230320",
            "cxt_third_buy_V230228",
            "cxt_third_sell_V230228",
            "cxt_five_bi_V230619",
            "cxt_seven_bi_V230620",
        ],
        "event_min_independent_occurrences": 1,
        "state_min_coverage": 0.8,
        "state_min_active_days": 30,
        "state_max_active_ratio": 0.9,
        "interaction_count": 4,
        "maximum_active_factors": 18,
        "minimum_absolute_weight": 0.0125,
        "minimum_trend_weight": 0.1,
        "protect_volume_window": True,
        "parallel_backend": "loky",
        "parallel_n_jobs_choices": [1, 2, 4, 8],
        "inner_max_num_threads": 1,
        "position_cache": "existing_target_digest",
        "staged_search": False,
    }


def test_protocol_and_grid_freeze_exact_sixty_four_configs() -> None:
    protocol = _protocol()

    validate_factor_protocol(protocol)
    specs = build_factor_specs(protocol)

    assert len(specs) == 64
    assert len({spec.spec_id for spec in specs}) == 64
    assert all(spec.exit < spec.enter for spec in specs)

    protocol["selection_metric"] = "sharpe"
    with pytest.raises(ValueError, match="return only"):
        validate_factor_protocol(protocol)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("event_min_independent_occurrences", 2, "event minimum"),
        ("parallel_backend", "threading", "loky"),
        ("parallel_n_jobs_choices", [1, 2], "job choices"),
        ("inner_max_num_threads", 2, "inner threads"),
        ("staged_search", True, "staged search"),
    ],
)
def test_protocol_rejects_changed_event_or_parallel_boundaries(field, value, message) -> None:
    protocol = deepcopy(_protocol())
    protocol[field] = value

    with pytest.raises(ValueError, match=message):
        validate_factor_protocol(protocol)


def test_rolling_fit_tasks_are_exact_and_deterministic() -> None:
    tasks = _rolling_fit_tasks(_protocol())

    assert len(tasks) == 32
    assert len({task.key for task in tasks}) == 32
    assert tasks == tuple(sorted(tasks, key=lambda task: task.key))


def test_fit_tasks_parallel_matches_serial() -> None:
    tasks = (
        RollingFitTask(0.025, 1, "2022H1"),
        RollingFitTask(0.025, 1, "2022H2"),
    )

    def fake_worker(task: RollingFitTask, inputs: object):
        del inputs
        return task.key, pd.Series({"base": 1.0, "event": task.step})

    serial = _fit_rolling_weights(None, tasks, n_jobs=1, worker=fake_worker)
    parallel = _fit_rolling_weights(None, tasks, n_jobs=2, worker=fake_worker)

    assert list(serial) == list(parallel)
    for key in serial:
        pd.testing.assert_series_equal(serial[key], parallel[key], check_exact=True)


def test_parallel_benchmark_requires_exact_weight_equivalence() -> None:
    tasks = (
        RollingFitTask(0.025, 1, "2022H1"),
        RollingFitTask(0.025, 1, "2022H2"),
    )

    def fake_worker(task: RollingFitTask, inputs: object):
        del inputs
        return task.key, pd.Series({"base": 1.0, "event": task.step})

    result = _benchmark_parallel_jobs(
        None,
        tasks,
        choices=(1, 2),
        worker=fake_worker,
    )

    assert result["backend"] == "loky"
    assert result["choices"] == [1, 2]
    assert result["selected_n_jobs"] in {1, 2}
    assert result["serial_parallel_equivalent"] is True
    assert set(result["timings_seconds"]) == {"1", "2"}


def test_rolling_fit_inputs_round_trip_read_only_factor_values() -> None:
    index = pd.date_range("2021-01-01", periods=3, freq="D")
    factors = pd.DataFrame({"base": [1.0, 0.0, -1.0], "event": [0.0, 1.0, 0.0]}, index=index)
    origin = pd.Series({"base": 1.0, "event": 0.0})
    target = pd.Series([0.0, 1.0, 1.0], index=index)
    daily = pd.DataFrame({"dt": index, "open": [1.0] * 3, "close": [1.0] * 3})

    inputs = RollingFitInputs.from_frames(
        factors,
        origin,
        target,
        ex04_enter=0.2,
        ex04_exit=0.0,
        state_rule=None,
        daily=daily,
        periods={"2021H1": (index[0], index[-1])},
        fee_rate=0.0005,
        init_cash=1_000_000,
        protocol=_protocol(),
    )
    rebuilt_factors, rebuilt_origin, rebuilt_target = inputs.frames()

    assert inputs.factor_values.flags.writeable is False
    pd.testing.assert_frame_equal(rebuilt_factors, factors)
    pd.testing.assert_series_equal(rebuilt_origin, origin)
    pd.testing.assert_series_equal(rebuilt_target, target)


def test_rolling_fit_worker_uses_only_training_windows_before_validation(monkeypatch) -> None:
    index = pd.date_range("2021-01-01", periods=3, freq="D")
    inputs = RollingFitInputs.from_frames(
        pd.DataFrame({"base": [1.0, 0.0, -1.0]}, index=index),
        pd.Series({"base": 1.0}),
        pd.Series([0.0, 1.0, 1.0], index=index),
        ex04_enter=0.2,
        ex04_exit=0.0,
        state_rule=None,
        daily=pd.DataFrame({"dt": index, "open": [1.0] * 3, "close": [1.0] * 3}),
        periods={
            "2021H1": (pd.Timestamp("2021-01-01"), pd.Timestamp("2021-06-30")),
            "2021H2": (pd.Timestamp("2021-07-01"), pd.Timestamp("2021-12-31")),
            "2022H1": (pd.Timestamp("2022-01-01"), pd.Timestamp("2022-06-30")),
        },
        fee_rate=0.0005,
        init_cash=1_000_000,
        protocol=_protocol(),
    )
    captured_periods = {}

    def fake_fit(factors, origin, target, enter, exit_, state_rule, evaluator, spec, protocol):
        del factors, target, enter, exit_, state_rule, spec, protocol
        captured_periods.update(evaluator.periods)
        return origin.rename("weight")

    monkeypatch.setattr("czsc_trader.factor_discovery_runner._fit_weights", fake_fit)
    key, weights = _run_rolling_fit_task(RollingFitTask(0.025, 1, "2022H1"), inputs)

    assert key == (0.025, 1, "2022H1")
    assert set(captured_periods) == {"2021H1", "2021H2"}
    assert weights.attrs["cache_stats"] == {"hits": 0, "misses": 0, "entries": 0}


def test_training_windows_are_strictly_before_validation() -> None:
    periods = half_year_periods(2021, 2025)

    assert training_windows_before(periods, "2022H1") == ("2021H1", "2021H2")
    assert all(periods[name][1] < periods["2024H2"][0] for name in training_windows_before(periods, "2024H2"))


def test_holdout_pass_uses_only_ex04_return() -> None:
    windows = {
        name: {
            "ex04_return": 0.10,
            "challenger_return": 0.11,
            "ex04_sharpe": 100.0,
            "challenger_sharpe": -100.0,
        }
        for name in ("2026Q1", "2026H1", "2026M1-M8")
    }

    assert factor_holdout_pass(windows)
    windows["2026H1"]["challenger_return"] = 0.10
    assert not factor_holdout_pass(windows)


def test_event_signal_support_is_written_from_candidate_metadata(tmp_path) -> None:
    candidate = CandidateFactors(
        pd.DataFrame(),
        pd.Series(dtype=float),
        {"signal_support": {"cxt_first_buy_V221126": {"status": "observed"}}},
    )

    _write_event_signal_support(tmp_path, candidate)

    payload = __import__("json").loads((tmp_path / "event_signal_support.json").read_text(encoding="utf-8"))
    assert payload["cxt_first_buy_V221126"]["status"] == "observed"


def test_comparison_window_reports_four_baselines_and_challenger() -> None:
    ex04 = {"strategy_return": 0.20, "sharpe": 2.0, "exposure": 0.5, "max_drawdown": -0.1, "trade_count": 4}
    ex05 = {"strategy_return": 0.19, "sharpe": 1.9, "exposure": 0.45, "max_drawdown": -0.09, "trade_count": 5}
    legacy = {"strategy_return": 0.25, "sharpe": 2.5, "exposure": 0.6, "max_drawdown": -0.2, "trade_count": 6}
    buyhold = {"strategy_return": 0.10, "sharpe": 1.0, "exposure": 1.0, "max_drawdown": -0.3, "trade_count": 1}
    challenger = {"strategy_return": 0.21, "sharpe": -9.0, "exposure": 0.4, "max_drawdown": -0.05, "trade_count": 3}

    row = build_comparison_window(ex04, ex05, legacy, buyhold, challenger)

    assert set(row) >= {"ex04", "ex05", "legacy_champion", "buyhold", "challenger"}
    assert row["return_delta_vs_ex04"] == pytest.approx(0.01)
    assert row["pass"] is True
    assert row["challenger"]["sharpe"] == -9.0
