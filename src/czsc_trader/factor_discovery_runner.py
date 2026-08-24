"""Run preregistered EX05 state-expanded factor discovery."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
from time import perf_counter
from typing import Any

import joblib
from joblib import Parallel, delayed, parallel_config
import numpy as np
import pandas as pd

from .audit import audit_no_lookahead
from .backtest import run_period_backtests
from .baselines import resolve_baseline
from .data import load_market_data
from .experiment_archive import build_experiment_manifest, validate_experiment_archive
from .factor_discovery import (
    CandidateFactors,
    generate_candidate_factors,
    rank_factor_results,
    sparse_coordinate_optimize,
    validate_sparse_weights,
)
from .factors import generate_factor_frame
from .four_layer import normalized_signal_factors, positions_from_scores, score_four_layer
from .four_layer_runner import _PeriodEvaluator, _events, _metrics, _write_json
from .objectives import TARGET_PERIODS
from .return_only_runner import VALIDATION_WINDOWS, half_year_periods
from .rules import FACTOR_COLUMNS, positions_for_rule


SELECTION_CUTOFF = pd.Timestamp("2025-12-31")


@dataclass(frozen=True)
class FactorSpec:
    step: float
    rounds: int
    enter: float
    exit: float

    @property
    def spec_id(self) -> str:
        return f"step{self.step:.3f}_r{self.rounds}_en{self.enter:+.3f}_ex{self.exit:+.3f}"


@dataclass(frozen=True, order=True)
class RollingFitTask:
    step: float
    rounds: int
    validation: str

    @property
    def key(self) -> tuple[float, int, str]:
        return self.step, self.rounds, self.validation


@dataclass(frozen=True)
class RollingFitInputs:
    factor_values: np.ndarray
    factor_index: pd.DatetimeIndex
    factor_names: tuple[str, ...]
    origin_values: np.ndarray
    origin_name: str | None
    ex04_target_values: np.ndarray
    ex04_target_name: str | None
    ex04_enter: float
    ex04_exit: float
    state_rule: Any
    daily: pd.DataFrame
    periods: dict[str, tuple[pd.Timestamp, pd.Timestamp]]
    fee_rate: float
    init_cash: float
    protocol: dict[str, object]

    @classmethod
    def from_frames(
        cls,
        factors: pd.DataFrame,
        origin: pd.Series,
        ex04_target: pd.Series,
        *,
        ex04_enter: float,
        ex04_exit: float,
        state_rule: Any,
        daily: pd.DataFrame,
        periods: Mapping[str, tuple[pd.Timestamp, pd.Timestamp]],
        fee_rate: float,
        init_cash: float,
        protocol: Mapping[str, object],
    ) -> "RollingFitInputs":
        factor_values = np.ascontiguousarray(factors.to_numpy(dtype=float))
        factor_values.setflags(write=False)
        origin_values = np.ascontiguousarray(origin.to_numpy(dtype=float))
        origin_values.setflags(write=False)
        target_values = np.ascontiguousarray(ex04_target.to_numpy(dtype=float))
        target_values.setflags(write=False)
        return cls(
            factor_values=factor_values,
            factor_index=pd.DatetimeIndex(factors.index),
            factor_names=tuple(map(str, factors.columns)),
            origin_values=origin_values,
            origin_name=origin.name,
            ex04_target_values=target_values,
            ex04_target_name=ex04_target.name,
            ex04_enter=float(ex04_enter),
            ex04_exit=float(ex04_exit),
            state_rule=state_rule,
            daily=daily.copy(),
            periods=dict(periods),
            fee_rate=float(fee_rate),
            init_cash=float(init_cash),
            protocol=dict(protocol),
        )

    def frames(self) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
        factors = pd.DataFrame(
            self.factor_values,
            index=self.factor_index,
            columns=self.factor_names,
            copy=False,
        )
        origin = pd.Series(
            self.origin_values,
            index=self.factor_names,
            name=self.origin_name,
            copy=False,
        )
        target = pd.Series(
            self.ex04_target_values,
            index=self.factor_index,
            name=self.ex04_target_name,
            copy=False,
        )
        return factors, origin, target


def _rolling_fit_tasks(protocol: Mapping[str, object]) -> tuple[RollingFitTask, ...]:
    return tuple(
        sorted(
            RollingFitTask(float(step), int(rounds), str(validation))
            for step in protocol["weight_steps"]
            for rounds in protocol["coordinate_rounds"]
            for validation in protocol["validation_windows"]
        )
    )


def _fit_rolling_weights(
    inputs: Any,
    tasks: Sequence[RollingFitTask],
    n_jobs: int,
    *,
    worker: Callable[[RollingFitTask, Any], tuple[tuple[float, int, str], pd.Series]] | None = None,
) -> dict[tuple[float, int, str], pd.Series]:
    if n_jobs not in {1, 2, 4, 8}:
        raise ValueError("n_jobs must be one of 1, 2, 4, 8")
    ordered = tuple(sorted(tasks, key=lambda task: task.key))
    expected_keys = tuple(task.key for task in ordered)
    if len(set(expected_keys)) != len(expected_keys):
        raise ValueError("rolling fit task keys must be unique")
    runner = _run_rolling_fit_task if worker is None else worker
    with parallel_config(
        backend="loky",
        inner_max_num_threads=1,
        max_nbytes="100K",
        mmap_mode="r",
    ):
        rows = Parallel(n_jobs=n_jobs)(delayed(runner)(task, inputs) for task in ordered)
    if len(rows) != len(expected_keys):
        raise RuntimeError("rolling fit task result count differs from request")
    result = dict(sorted(rows, key=lambda row: row[0]))
    if tuple(result) != expected_keys:
        raise RuntimeError("rolling fit task keys are missing, duplicated, or unknown")
    return result


def _benchmark_parallel_jobs(
    inputs: Any,
    tasks: Sequence[RollingFitTask],
    choices: Sequence[int],
    *,
    worker: Callable[[RollingFitTask, Any], tuple[tuple[float, int, str], pd.Series]] | None = None,
) -> dict[str, object]:
    timings: dict[str, float] = {}
    reference: dict[tuple[float, int, str], pd.Series] | None = None
    equivalent = True
    for n_jobs in choices:
        started = perf_counter()
        result = _fit_rolling_weights(inputs, tasks, int(n_jobs), worker=worker)
        timings[str(n_jobs)] = perf_counter() - started
        if reference is None:
            reference = result
        elif tuple(result) != tuple(reference) or any(
            not result[key].equals(reference[key]) for key in reference
        ):
            equivalent = False
    if not equivalent:
        raise AssertionError("serial and parallel rolling weights differ")
    selected = min((seconds, int(n_jobs)) for n_jobs, seconds in timings.items())[1]
    return {
        "backend": "loky",
        "joblib_version": joblib.__version__,
        "choices": [int(value) for value in choices],
        "selected_n_jobs": selected,
        "serial_parallel_equivalent": equivalent,
        "timings_seconds": timings,
    }


def build_factor_specs(protocol: Mapping[str, object]) -> tuple[FactorSpec, ...]:
    return tuple(
        FactorSpec(float(step), int(rounds), float(enter), float(exit_))
        for step in protocol["weight_steps"]
        for rounds in protocol["coordinate_rounds"]
        for enter in protocol["enter_thresholds"]
        for exit_ in protocol["exit_thresholds"]
        if float(exit_) < float(enter)
    )


def training_windows_before(
    periods: Mapping[str, tuple[pd.Timestamp, pd.Timestamp]], validation: str
) -> tuple[str, ...]:
    start = periods[validation][0]
    return tuple(name for name, (_, end) in periods.items() if end < start)


def validate_factor_protocol(protocol: Mapping[str, object]) -> None:
    if protocol.get("experiment_type") != "event_aware_parallel_factor_discovery":
        raise ValueError("not an EX06 factor-discovery protocol")
    if protocol.get("status") != "PRE_REGISTERED":
        raise ValueError("protocol must remain PRE_REGISTERED")
    if protocol.get("selection_sample_end") != "2025-12-31":
        raise ValueError("selection must stop at 2025-12-31")
    if tuple(protocol.get("validation_windows", [])) != VALIDATION_WINDOWS:
        raise ValueError("validation windows differ from EX05 protocol")
    if tuple(protocol.get("holdout_windows", [])) != tuple(TARGET_PERIODS):
        raise ValueError("holdout windows differ from objectives")
    if protocol.get("selection_metric") != "strategy_return_only":
        raise ValueError("EX06 selection must use return only")
    if protocol.get("holdout_access_before_freeze") is not False:
        raise ValueError("holdout access must be forbidden before freeze")
    new_daily_signals = tuple(map(str, protocol.get("new_daily_signals", [])))
    if len(new_daily_signals) != 13 or "cxt_first_sell_V221126" not in new_daily_signals:
        raise ValueError("EX06 must add first sell to the 12 EX05 daily signals")
    expected_events = (
        "cxt_first_buy_V221126",
        "cxt_first_sell_V221126",
        "cxt_second_buy_V230320",
        "cxt_second_sell_V230320",
        "cxt_third_buy_V230228",
        "cxt_third_sell_V230228",
        "cxt_five_bi_V230619",
        "cxt_seven_bi_V230620",
    )
    if tuple(map(str, protocol.get("event_signal_requirements", []))) != expected_events:
        raise ValueError("EX06 event signal requirements differ from preregistration")
    if int(protocol.get("event_min_independent_occurrences", -1)) != 1:
        raise ValueError("EX06 event minimum must be one independent occurrence")
    if protocol.get("parallel_backend") != "loky":
        raise ValueError("EX06 parallel backend must be loky")
    if tuple(protocol.get("parallel_n_jobs_choices", [])) != (1, 2, 4, 8):
        raise ValueError("EX06 parallel job choices must be 1, 2, 4, 8")
    if int(protocol.get("inner_max_num_threads", -1)) != 1:
        raise ValueError("EX06 inner threads must equal one")
    if protocol.get("position_cache") != "existing_target_digest":
        raise ValueError("EX06 must reuse the existing target digest cache")
    if protocol.get("staged_search") is not False:
        raise ValueError("EX06 staged search must remain disabled")
    if tuple(protocol.get("weight_steps", [])) != (0.025, 0.05):
        raise ValueError("EX06 weight steps differ from EX05")
    if tuple(protocol.get("coordinate_rounds", [])) != (1, 2):
        raise ValueError("EX06 coordinate rounds differ from EX05")
    if tuple(protocol.get("enter_thresholds", [])) != (0.125, 0.15, 0.175, 0.2):
        raise ValueError("EX06 enter thresholds differ from EX05")
    if tuple(protocol.get("exit_thresholds", [])) != (-0.025, 0.0, 0.025, 0.05):
        raise ValueError("EX06 exit thresholds differ from EX05")
    if int(protocol.get("interaction_count", -1)) != 4:
        raise ValueError("EX05 must contain four interactions")
    specs = build_factor_specs(protocol)
    if len(specs) != int(protocol.get("algorithm_config_count", -1)) or len(specs) != 64:
        raise ValueError("EX05 must contain exactly 64 algorithm configurations")


def factor_holdout_pass(windows: Mapping[str, Mapping[str, object]]) -> bool:
    return set(windows) == set(TARGET_PERIODS) and all(
        float(row["challenger_return"]) > float(row["ex04_return"])
        for row in windows.values()
    )


def _metric_view(metrics: Mapping[str, object]) -> dict[str, object]:
    keys = ("strategy_return", "sharpe", "max_drawdown", "exposure", "trade_count")
    return {key: metrics[key] for key in keys}


def build_comparison_window(
    ex04: Mapping[str, object],
    ex05: Mapping[str, object],
    legacy: Mapping[str, object],
    buyhold: Mapping[str, object],
    challenger: Mapping[str, object],
) -> dict[str, object]:
    ex04_return = float(ex04["strategy_return"])
    challenger_return = float(challenger["strategy_return"])
    return {
        "ex04": _metric_view(ex04),
        "ex05": _metric_view(ex05),
        "legacy_champion": _metric_view(legacy),
        "buyhold": _metric_view(buyhold),
        "challenger": _metric_view(challenger),
        "ex04_return": ex04_return,
        "challenger_return": challenger_return,
        "return_delta_vs_ex04": challenger_return - ex04_return,
        "ex04_sharpe": float(ex04["sharpe"]),
        "challenger_sharpe": float(challenger["sharpe"]),
        "sharpe_delta_vs_ex04": float(challenger["sharpe"]) - float(ex04["sharpe"]),
        "pass": challenger_return > ex04_return,
    }


def _write_event_signal_support(artifacts: Path, candidate: CandidateFactors) -> None:
    support = candidate.metadata.get("signal_support")
    if not isinstance(support, dict) or not support:
        raise ValueError("EX06 candidate metadata is missing event signal support")
    _write_json(Path(artifacts) / "event_signal_support.json", support)


def _load_ex04(path: Path, protocol: Mapping[str, object]) -> dict[str, object]:
    path = Path(path)
    expected = str(protocol["research_baseline"]["sha256"])
    actual = sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError("EX04 frozen challenger hash differs from preregistration")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("experiment") != "0824_EX04" or payload.get("selection_metric") != "strategy_return_only":
        raise ValueError("research baseline is not the frozen EX04 challenger")
    return payload


def _load_ex05(path: Path, protocol: Mapping[str, object]) -> dict[str, object]:
    path = Path(path)
    expected = str(protocol["ex05_reference"]["sha256"])
    actual = sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError("EX05 frozen challenger hash differs from preregistration")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("experiment") != "0824_EX05" or payload.get("selection_metric") != "strategy_return_only":
        raise ValueError("comparison reference is not the frozen EX05 challenger")
    return payload


def _target(
    factors: pd.DataFrame,
    weights: pd.Series,
    enter: float,
    exit_: float,
    state_rule: Any,
) -> tuple[pd.Series, pd.Series]:
    scores = score_four_layer(factors.loc[:, list(weights.index)], weights)
    return positions_from_scores(scores, enter, exit_, state_rule), scores


def _return_objective(
    baseline: Mapping[str, Mapping[str, object]],
    challenger: Mapping[str, Mapping[str, object]],
    weights: pd.Series,
    origin: pd.Series,
) -> tuple[float, ...]:
    deltas = [
        float(challenger[name]["strategy_return"]) - float(baseline[name]["strategy_return"])
        for name in baseline
    ]
    return (
        float(sum(delta > 0.0 for delta in deltas)),
        min(deltas),
        float(np.median(deltas)),
        float(np.mean(deltas)),
        -float(weights.ne(0.0).sum()),
        -float((weights - origin).abs().sum()),
    )


def _fit_weights(
    factors: pd.DataFrame,
    origin: pd.Series,
    ex04_target: pd.Series,
    ex04_enter: float,
    ex04_exit: float,
    state_rule: Any,
    evaluator: _PeriodEvaluator,
    spec: FactorSpec,
    protocol: Mapping[str, object],
) -> pd.Series:
    baseline = evaluator.evaluate(ex04_target)

    def evaluate(weights: pd.Series) -> tuple[float, ...]:
        target, _ = _target(factors, weights, ex04_enter, ex04_exit, state_rule)
        return _return_objective(baseline, evaluator.evaluate(target), weights, origin)

    fitted = sparse_coordinate_optimize(
        origin,
        spec.step,
        spec.rounds,
        protocol,
        evaluate,
    )
    validate_sparse_weights(fitted, factors.columns, protocol)
    return fitted


def _run_rolling_fit_task(
    task: RollingFitTask,
    inputs: RollingFitInputs,
) -> tuple[tuple[float, int, str], pd.Series]:
    factors, origin, ex04_target = inputs.frames()
    training_names = training_windows_before(inputs.periods, task.validation)
    training_periods = {name: inputs.periods[name] for name in training_names}
    evaluator = _PeriodEvaluator(
        inputs.daily,
        training_periods,
        inputs.fee_rate,
        inputs.init_cash,
    )
    weights = _fit_weights(
        factors,
        origin,
        ex04_target,
        inputs.ex04_enter,
        inputs.ex04_exit,
        inputs.state_rule,
        evaluator,
        FactorSpec(task.step, task.rounds, inputs.ex04_enter, inputs.ex04_exit),
        inputs.protocol,
    )
    weights.attrs["cache_stats"] = evaluator.cache_stats
    return task.key, weights


def _candidate_rows(candidate: CandidateFactors) -> pd.DataFrame:
    state_lookup = {
        str(row["factor"]): row for row in candidate.metadata.get("states", [])
    }
    rows: list[dict[str, object]] = []
    for name in candidate.factors.columns:
        if name.startswith("state__"):
            row = state_lookup[name]
            rows.append({"factor": name, "type": "state", **row, "origin_weight": 0.0})
        elif name.startswith("interaction__"):
            rows.append({"factor": name, "type": "interaction", "origin_weight": 0.0})
        else:
            rows.append(
                {
                    "factor": name,
                    "type": "ex04_base",
                    "origin_weight": float(candidate.origin_weights.loc[name]),
                }
            )
    return pd.DataFrame(rows)


def _selection(
    data: Any,
    candidate: CandidateFactors,
    ex04: Mapping[str, object],
    state_rule: Any,
    protocol: Mapping[str, object],
    artifacts: Path,
    fee_rate: float,
    init_cash: float,
) -> tuple[FactorSpec, pd.Series, dict[str, object]]:
    factors, origin = candidate.factors, candidate.origin_weights
    ex04_weights = pd.Series(ex04["weights"], dtype=float)
    ex04_enter, ex04_exit = float(ex04["spec"]["enter"]), float(ex04["spec"]["exit"])
    ex04_target, ex04_scores = _target(
        factors.loc[:, list(ex04_weights.index)], ex04_weights, ex04_enter, ex04_exit, state_rule
    )
    origin_target, origin_scores = _target(factors, origin, ex04_enter, ex04_exit, state_rule)
    proof = {
        "status": "PASS",
        "max_score_absolute_error": float((origin_scores - ex04_scores).abs().max()),
        "positions_equal": bool(origin_target.equals(ex04_target)),
        "base_factor_count": len(ex04_weights),
        "candidate_factor_count": len(factors.columns),
    }
    if proof["max_score_absolute_error"] > 1e-12 or not proof["positions_equal"]:
        raise AssertionError(f"EX04 candidate-universe equivalence failed: {proof}")
    _write_json(artifacts / "equivalence_proof.json", proof)
    _write_json(artifacts / "factor_universe.json", candidate.metadata)
    _candidate_rows(candidate).to_csv(
        artifacts / "factor_candidates.csv", index=False, encoding="utf-8-sig"
    )

    periods = half_year_periods(2021, 2025)
    validation_periods = {name: periods[name] for name in VALIDATION_WINDOWS}
    validation_evaluator = _PeriodEvaluator(data.daily, validation_periods, fee_rate, init_cash)
    ex04_validation = validation_evaluator.evaluate(ex04_target)
    specs = build_factor_specs(protocol)
    rolling_inputs = RollingFitInputs.from_frames(
        factors,
        origin,
        ex04_target,
        ex04_enter=ex04_enter,
        ex04_exit=ex04_exit,
        state_rule=state_rule,
        daily=data.daily,
        periods=periods,
        fee_rate=fee_rate,
        init_cash=init_cash,
        protocol=protocol,
    )
    all_tasks = _rolling_fit_tasks(protocol)
    benchmark_tasks = tuple(
        task
        for task in all_tasks
        if task.step == 0.025
        and task.rounds == 1
        and task.validation in VALIDATION_WINDOWS[:4]
    )
    benchmark = _benchmark_parallel_jobs(
        rolling_inputs,
        benchmark_tasks,
        tuple(int(value) for value in protocol.get("parallel_n_jobs_choices", [1])),
    )
    _write_json(artifacts / "parallel_benchmark.json", benchmark)
    fitted_cache = _fit_rolling_weights(
        rolling_inputs,
        all_tasks,
        int(benchmark["selected_n_jobs"]),
    )
    rows: list[dict[str, object]] = []
    for spec in specs:
        metrics: dict[str, dict[str, object]] = {}
        active_counts: list[int] = []
        shifts: list[float] = []
        for validation in VALIDATION_WINDOWS:
            key = (spec.step, spec.rounds, validation)
            weights = fitted_cache[key]
            target, _ = _target(factors, weights, spec.enter, spec.exit, state_rule)
            metrics[validation] = validation_evaluator.evaluate(target)[validation]
            active_counts.append(int(weights.ne(0.0).sum()))
            shifts.append(float((weights - origin).abs().sum()))
        deltas = {
            name: float(metrics[name]["strategy_return"])
            - float(ex04_validation[name]["strategy_return"])
            for name in VALIDATION_WINDOWS
        }
        row: dict[str, object] = {
            **asdict(spec),
            "spec_id": spec.spec_id,
            "win_count": sum(delta > 0.0 for delta in deltas.values()),
            "min_return_delta": min(deltas.values()),
            "median_return_delta": float(np.median(list(deltas.values()))),
            "mean_return_delta": float(np.mean(list(deltas.values()))),
            "mean_active_factors": float(np.mean(active_counts)),
            "weight_shift": float(np.mean(shifts)),
        }
        for name in VALIDATION_WINDOWS:
            row[f"{name}_return"] = metrics[name]["strategy_return"]
            row[f"{name}_return_delta"] = deltas[name]
            row[f"{name}_sharpe"] = metrics[name]["sharpe"]
            row[f"{name}_win"] = deltas[name] > 0.0
        rows.append(row)
    ranked = rank_factor_results(pd.DataFrame(rows))
    ranked.to_csv(artifacts / "candidate_results.csv", index=False, encoding="utf-8-sig")
    best_id = str(ranked.iloc[0]["spec_id"])
    best = next(spec for spec in specs if spec.spec_id == best_id)
    final_evaluator = _PeriodEvaluator(data.daily, periods, fee_rate, init_cash)
    final_weights = _fit_weights(
        factors,
        origin,
        ex04_target,
        ex04_enter,
        ex04_exit,
        state_rule,
        final_evaluator,
        best,
        protocol,
    )
    final_weights.rename_axis("factor").reset_index().assign(
        active=lambda frame: frame["weight"].ne(0.0)
    ).to_csv(artifacts / "factor_weights.csv", index=False, encoding="utf-8-sig")
    rolling_cache_stats = {
        key: sum(int(weights.attrs.get("cache_stats", {}).get(key, 0)) for weights in fitted_cache.values())
        for key in ("hits", "misses", "entries")
    }
    summary = {
        "best_spec": best_id,
        "candidate_count": len(ranked),
        "candidate_factor_count": len(factors.columns),
        "best_win_count": int(ranked.iloc[0]["win_count"]),
        "best_min_return_delta": float(ranked.iloc[0]["min_return_delta"]),
        "best_median_return_delta": float(ranked.iloc[0]["median_return_delta"]),
        "best_mean_return_delta": float(ranked.iloc[0]["mean_return_delta"]),
        "final_active_factor_count": int(final_weights.ne(0.0).sum()),
        "final_weight_shift": float((final_weights - origin).abs().sum()),
        "selection_cutoff": "2025-12-31",
        "visible_data_hashes": data.hashes,
        "holdout_accessed": False,
        "parallel": benchmark,
        "cache_stats": {
            "rolling_fits": rolling_cache_stats,
            "validation": validation_evaluator.cache_stats,
            "final_fit": final_evaluator.cache_stats,
        },
    }
    _write_json(artifacts / "selection_metrics.json", summary)
    return best, final_weights, summary


def _holdout(
    raw_dir: Path,
    baseline: Any,
    ex04: Mapping[str, object],
    ex05: Mapping[str, object],
    frozen: Mapping[str, object],
    frozen_digest: str,
    protocol: Mapping[str, object],
    artifacts: Path,
    fee_rate: float,
    init_cash: float,
) -> dict[str, object]:
    cutoff = max(end for _, end in TARGET_PERIODS.values())
    data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=cutoff)
    ex04_weights = pd.Series(ex04["weights"], dtype=float)
    ex05_weights = pd.Series(ex05["weights"], dtype=float)
    active_weights = pd.Series(frozen["weights"], dtype=float)
    replay_names = list(dict.fromkeys([*active_weights.index, *ex05_weights.index]))
    candidate = generate_candidate_factors(
        data,
        protocol,
        ex04_weights,
        frozen_names=replay_names,
    )
    validate_sparse_weights(active_weights, active_weights.index, protocol)
    challenger_target, challenger_scores = _target(
        candidate.factors.loc[:, list(active_weights.index)],
        active_weights,
        float(frozen["enter"]),
        float(frozen["exit"]),
        baseline.rule,
    )
    validate_sparse_weights(ex05_weights, ex05_weights.index, protocol)
    ex05_target, _ = _target(
        candidate.factors.loc[:, list(ex05_weights.index)],
        ex05_weights,
        float(ex05["enter"]),
        float(ex05["exit"]),
        baseline.rule,
    )

    legacy_frame = generate_factor_frame(data).frame
    existing = normalized_signal_factors(legacy_frame.filter(like="raw__"))
    ex04_target, _ = _target(
        existing,
        ex04_weights,
        float(ex04["spec"]["enter"]),
        float(ex04["spec"]["exit"]),
        baseline.rule,
    )
    legacy_target, _ = positions_for_rule(legacy_frame.loc[:, FACTOR_COLUMNS], baseline.rule)
    buyhold_target = pd.Series(1.0, index=challenger_target.index, name="target_position")

    ex04_results = run_period_backtests(
        data.daily, ex04_target, TARGET_PERIODS, fee_rate=fee_rate, init_cash=init_cash
    )
    ex05_results = run_period_backtests(
        data.daily, ex05_target, TARGET_PERIODS, fee_rate=fee_rate, init_cash=init_cash
    )
    legacy_results = run_period_backtests(
        data.daily, legacy_target, TARGET_PERIODS, fee_rate=fee_rate, init_cash=init_cash
    )
    buyhold_results = run_period_backtests(
        data.daily, buyhold_target, TARGET_PERIODS, fee_rate=fee_rate, init_cash=init_cash
    )
    events = _events(
        challenger_target,
        challenger_scores,
        float(frozen["enter"]),
        float(frozen["exit"]),
    )
    audit_frame = pd.DataFrame(
        {
            "factor_score": challenger_scores,
            "enter_threshold": float(frozen["enter"]),
            "exit_threshold": float(frozen["exit"]),
        },
        index=challenger_scores.index,
    )
    challenger_results = run_period_backtests(
        data.daily,
        challenger_target,
        TARGET_PERIODS,
        fee_rate=fee_rate,
        init_cash=init_cash,
        factor_events=events,
        factor_frame=audit_frame,
    )
    ex04_metrics = _metrics(ex04_results)
    ex05_metrics = _metrics(ex05_results)
    legacy_metrics = _metrics(legacy_results)
    buyhold_metrics = _metrics(buyhold_results)
    challenger_metrics = _metrics(challenger_results)
    windows = {
        name: build_comparison_window(
            ex04_metrics[name],
            ex05_metrics[name],
            legacy_metrics[name],
            buyhold_metrics[name],
            challenger_metrics[name],
        )
        for name in TARGET_PERIODS
    }
    overall = factor_holdout_pass(windows)
    orders = pd.concat([result.orders for result in challenger_results.values()], ignore_index=True)
    used_events = pd.concat(
        [events, *[result.factor_events for result in challenger_results.values()]],
        ignore_index=True,
    ).drop_duplicates(subset=["event_id"])
    audit = audit_no_lookahead(orders, used_events, challenger_target, audit_frame)
    orders.to_csv(artifacts / "orders.csv", index=False, encoding="utf-8-sig")
    payload = {
        "status": "PASS" if overall else "FAIL",
        "windows": windows,
        "audit": audit,
        "frozen_challenger_sha256": frozen_digest,
        "frozen_before_holdout": True,
        "holdout_cutoff": str(cutoff.date()),
        "holdout_data_hashes": data.hashes,
        "holdout_accessed": True,
    }
    _write_json(artifacts / "holdout_metrics.json", payload)
    return payload


def _write_docs(
    experiment_dir: Path,
    selection: Mapping[str, object],
    holdout: Mapping[str, object],
    frozen: Mapping[str, object],
) -> None:
    (experiment_dir / "03_execution.md").write_text(
        "# 执行过程\n\n"
        f"- 状态：正式执行完成（`{holdout['status']}`）\n"
        f"- 候选因子：{selection['candidate_factor_count']}项\n"
        f"- 算法配置：{selection['candidate_count']}项\n"
        f"- 冻结配置：`{selection['best_spec']}`\n"
        f"- 滚动半年收益胜出：{selection['best_win_count']}/8\n"
        f"- 最终非零因子：{selection['final_active_factor_count']}项\n"
        "- 选优指标：仅收益率\n"
        "- 冻结前访问2026：否\n"
        "- 冻结后访问2026：是\n"
        f"- 因果审计：`{holdout['audit']['status']}`\n",
        encoding="utf-8",
    )
    lines = [
        "# 研究结论",
        "",
        f"EX06事件感知并行搜索结果为 **{holdout['status']}**。",
        "",
        "| 窗口 | EX04收益 | EX06收益 | 收益增量 | EX04夏普 | EX06夏普 | 判定 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for name in TARGET_PERIODS:
        row = holdout["windows"][name]
        lines.append(
            f"| {name} | {float(row['ex04_return']):.4%} | "
            f"{float(row['challenger_return']):.4%} | {float(row['return_delta_vs_ex04']):+.4%} | "
            f"{float(row['ex04_sharpe']):.4f} | {float(row['challenger_sharpe']):.4f} | "
            f"{'PASS' if row['pass'] else 'FAIL'} |"
        )
    lines.extend(
        [
            "",
            "## 冻结模型",
            "",
            f"- 非零因子：{len(frozen['factor_names'])}项。",
            f"- 入场阈值：{float(frozen['enter']):.3f}；离场阈值：{float(frozen['exit']):.3f}。",
            "- 夏普率只报告，未参与选优或PASS。",
            "- EX05、旧冠军与Buy & Hold完整指标见`artifacts/holdout_metrics.json`。",
            "- 2026结果未触发二次调参。",
        ]
    )
    (experiment_dir / "04_conclusion.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_factor_discovery_experiment(
    raw_dir: Path,
    baseline_root: Path,
    ex04_path: Path,
    experiment_dir: Path,
    *,
    ex05_path: Path | None = None,
    fee_rate: float = 0.0005,
    init_cash: float = 1_000_000.0,
) -> dict[str, object]:
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    validate_factor_protocol(protocol)
    ex04 = _load_ex04(ex04_path, protocol)
    if ex05_path is None:
        ex05_path = Path(str(protocol["ex05_reference"]["file"]))
    ex05 = _load_ex05(ex05_path, protocol)
    baseline = resolve_baseline(Path(baseline_root), str(protocol["legacy_champion"]["version"]))
    if baseline.sha256 != str(protocol["legacy_champion"]["sha256"]):
        raise ValueError("legacy champion hash differs from preregistration")

    selection_data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=SELECTION_CUTOFF)
    ex04_weights = pd.Series(ex04["weights"], dtype=float)
    candidate = generate_candidate_factors(selection_data, protocol, ex04_weights)
    _write_event_signal_support(artifacts, candidate)
    best, weights, selection = _selection(
        selection_data,
        candidate,
        ex04,
        baseline.rule,
        protocol,
        artifacts,
        fee_rate,
        init_cash,
    )
    active_weights = weights[weights.ne(0.0)]
    validate_sparse_weights(active_weights, active_weights.index, protocol)
    frozen = {
        "schema_version": 1,
        "experiment": experiment_dir.name,
        "sample_end": "2025-12-31",
        "research_baseline": protocol["research_baseline"],
        "factor_policy": "event_aware_state_expansion_with_sparse_add_drop_replace",
        "factor_names": list(active_weights.index),
        "weights": active_weights.to_dict(),
        "enter": best.enter,
        "exit": best.exit,
        "spec": asdict(best),
        "spec_id": best.spec_id,
        "selection_metric": "strategy_return_only",
    }
    frozen_path = artifacts / "frozen_challenger.json"
    _write_json(frozen_path, frozen)
    frozen_digest = sha256(frozen_path.read_bytes()).hexdigest()
    holdout = _holdout(
        raw_dir,
        baseline,
        ex04,
        ex05,
        frozen,
        frozen_digest,
        protocol,
        artifacts,
        fee_rate,
        init_cash,
    )
    _write_docs(experiment_dir, selection, holdout, frozen)
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": experiment_dir.name,
            "date": "2026-08-24",
            "status": holdout["status"],
            "symbol": "588080.SH",
            "asset_type": "etf",
            "research_baseline": protocol["research_baseline"],
            "legacy_champion": protocol["legacy_champion"],
            "visible_sample_end": "2025-12-31",
            "holdout_accessed": True,
            "frozen_challenger_sha256": frozen_digest,
        },
    )
    validate_experiment_archive(experiment_dir)
    return {
        "status": holdout["status"],
        "best_spec": best.spec_id,
        "selection": selection,
        "holdout": holdout,
        "experiment_dir": str(experiment_dir),
    }


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--baseline-root", type=Path, default=Path("configs/rule_baselines"))
    parser.add_argument(
        "--ex05-path",
        type=Path,
        default=Path("experiments/0824_EX05/artifacts/frozen_challenger.json"),
    )
    parser.add_argument(
        "--ex04-path",
        type=Path,
        default=Path("experiments/0824_EX04/artifacts/frozen_challenger.json"),
    )
    parser.add_argument("--experiment-dir", type=Path, default=Path("experiments/0824_EX06"))
    args = parser.parse_args()
    result = run_factor_discovery_experiment(
        args.raw_dir,
        args.baseline_root,
        args.ex04_path,
        args.experiment_dir,
        ex05_path=args.ex05_path,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    cli()
