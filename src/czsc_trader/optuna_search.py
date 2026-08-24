"""Pure strategy projection and ranking for the EX07 Optuna search."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Mapping, Sequence
import json
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np
import optuna
import pandas as pd

from .factor_discovery import validate_sparse_weights


@dataclass(frozen=True)
class ProjectedStrategy:
    """One valid sparse four-layer strategy decoded from trial parameters."""

    weights: pd.Series
    enter: float
    exit: float
    active_factor_count: int


@dataclass(frozen=True)
class TrialRequest:
    """Pickle-safe work request created from an Optuna trial."""

    number: int
    payload: Any


@dataclass(frozen=True)
class TrialOutcome:
    """Data-only worker result written to Optuna by the parent process."""

    number: int
    value: float | None
    user_attrs: Mapping[str, Any]
    error: str | None = None


def _bounds(protocol: Mapping[str, object], key: str) -> tuple[float, float]:
    values = protocol[key]
    if not isinstance(values, (list, tuple)) or len(values) != 2:
        raise ValueError(f"{key} must contain two bounds")
    return float(values[0]), float(values[1])


def validate_optuna_protocol(protocol: Mapping[str, object]) -> None:
    """Reject any change to the preregistered EX07 search boundary."""
    if protocol.get("experiment_type") != "optuna_joint_strategy_search":
        raise ValueError("not an EX07 Optuna protocol")
    if protocol.get("status") != "PRE_REGISTERED":
        raise ValueError("protocol must remain PRE_REGISTERED")
    if protocol.get("selection_sample_end") != "2025-12-31":
        raise ValueError("selection cutoff differs from preregistration")
    if protocol.get("selection_metric") != "minimum_return_delta_vs_ex04":
        raise ValueError("selection metric differs from preregistration")
    if protocol.get("holdout_access_before_freeze") is not False:
        raise ValueError("holdout must remain inaccessible before freeze")
    if protocol.get("active_factor_count") != {"low": 6, "high": 18}:
        raise ValueError("active factor count bounds differ from preregistration")
    if tuple(protocol.get("raw_weight_bounds", ())) != (-1.0, 1.0):
        raise ValueError("raw weight bounds differ from preregistration")
    if tuple(protocol.get("enter_threshold_bounds", ())) != (0.05, 0.3):
        raise ValueError("entry threshold bounds differ from preregistration")
    if tuple(protocol.get("exit_gap_bounds", ())) != (0.025, 0.25):
        raise ValueError("exit gap bounds differ from preregistration")
    optuna_config = protocol.get("optuna")
    if not isinstance(optuna_config, Mapping):
        raise ValueError("missing Optuna configuration")
    expected_optuna = {
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
    }
    if dict(optuna_config) != expected_optuna:
        raise ValueError("Optuna configuration differs from preregistration")
    parallel = protocol.get("parallel")
    expected_parallel = {
        "backend": "loky",
        "n_jobs": 8,
        "inner_max_num_threads": 1,
        "database_writer": "parent_only",
        "result_order": "trial_number",
    }
    if not isinstance(parallel, Mapping) or dict(parallel) != expected_parallel:
        raise ValueError("parallel configuration differs from preregistration")


def _protected_name(
    candidates: Sequence[str], raw: pd.Series
) -> str:
    if not candidates:
        raise ValueError("required protected factor is absent")
    return sorted(candidates, key=lambda name: (-abs(float(raw.loc[name])), name))[0]


def _replace_weakest(
    selected: list[str], required: str, protected: set[str], raw: pd.Series
) -> None:
    if required in selected:
        protected.add(required)
        return
    replaceable = [name for name in selected if name not in protected]
    if not replaceable:
        raise ValueError("no factor can be replaced by a protected factor")
    weakest = sorted(
        replaceable, key=lambda name: (abs(float(raw.loc[name])), name), reverse=False
    )[0]
    selected[selected.index(weakest)] = required
    protected.add(required)


def project_trial_parameters(
    factor_names: Sequence[str],
    raw_weights: pd.Series,
    active_factor_count: int,
    enter_threshold: float,
    exit_gap: float,
    origin_weights: pd.Series,
    protocol: Mapping[str, object],
) -> ProjectedStrategy:
    """Deterministically project dense trial values into a valid sparse strategy."""
    names = tuple(map(str, factor_names))
    if len(names) != len(set(names)) or not names:
        raise ValueError("factor names must be unique and non-empty")
    if set(raw_weights.index) != set(names):
        raise ValueError("raw weight identities differ from candidate factors")
    raw = raw_weights.reindex(names).astype(float)
    if not np.isfinite(raw).all():
        raise ValueError("raw weights must be finite")
    raw_low, raw_high = _bounds(protocol, "raw_weight_bounds")
    if raw.lt(raw_low).any() or raw.gt(raw_high).any():
        raise ValueError("raw weight lies outside bounds")
    count_bounds = protocol["active_factor_count"]
    if not isinstance(count_bounds, Mapping):
        raise ValueError("active factor count bounds are missing")
    low, high = int(count_bounds["low"]), int(count_bounds["high"])
    if not low <= int(active_factor_count) <= min(high, len(names)):
        raise ValueError("active factor count lies outside bounds")
    enter_low, enter_high = _bounds(protocol, "enter_threshold_bounds")
    if not enter_low <= float(enter_threshold) <= enter_high:
        raise ValueError("entry threshold lies outside bounds")
    gap_low, gap_high = _bounds(protocol, "exit_gap_bounds")
    if not gap_low <= float(exit_gap) <= gap_high:
        raise ValueError("exit gap lies outside bounds")

    ordered = sorted(names, key=lambda name: (-abs(float(raw.loc[name])), name))
    selected = ordered[: int(active_factor_count)]
    protected: set[str] = set()
    volume_names = [
        name for name in names if name.startswith("raw__daily__vol_window_V230731")
    ]
    if bool(protocol["protect_volume_window"]):
        volume = _protected_name(volume_names, raw)
        _replace_weakest(selected, volume, protected, raw)
    trend_names = [
        name
        for name in names
        if name.startswith("raw__") and ("tas_ma_" in name or "tas_macd_" in name)
    ]
    trend = _protected_name(trend_names, raw)
    _replace_weakest(selected, trend, protected, raw)
    selected = sorted(set(selected), key=lambda name: names.index(name))
    if len(selected) != int(active_factor_count):
        raise AssertionError("projection changed active factor count")

    minimum = float(protocol["minimum_absolute_weight"])
    residual = 1.0 - len(selected) * minimum
    if residual < 0.0:
        raise ValueError("minimum weight is infeasible for active factor count")
    strength = raw.loc[selected].abs()
    if float(strength.sum()) == 0.0:
        strength = pd.Series(1.0, index=selected)
    magnitudes = pd.Series(minimum, index=selected, dtype=float)
    magnitudes += residual * strength / float(strength.sum())

    selected_trends = [name for name in selected if name in trend_names]
    trend_mass = float(magnitudes.loc[selected_trends].sum())
    minimum_trend = float(protocol["minimum_trend_weight"])
    if trend_mass < minimum_trend:
        delta = minimum_trend - trend_mass
        nontrend = [name for name in selected if name not in selected_trends]
        slack = magnitudes.loc[nontrend] - minimum
        if float(slack.sum()) + 1e-15 < delta:
            raise ValueError("insufficient non-trend weight to protect trend factors")
        magnitudes.loc[nontrend] -= delta * slack / float(slack.sum())
        trend_strength = magnitudes.loc[selected_trends]
        magnitudes.loc[selected_trends] += delta * trend_strength / float(trend_strength.sum())

    origin = origin_weights.reindex(names).fillna(0.0).astype(float)
    signs = pd.Series(1.0, index=selected)
    for name in selected:
        value = float(raw.loc[name])
        fallback = float(origin.loc[name])
        signs.loc[name] = np.sign(value) if value != 0.0 else (np.sign(fallback) or 1.0)
    weights = pd.Series(0.0, index=names, name="weight")
    weights.loc[selected] = magnitudes * signs

    validation_protocol = dict(protocol)
    validation_protocol["maximum_active_factors"] = high
    validate_sparse_weights(weights, names, validation_protocol)
    return ProjectedStrategy(
        weights=weights,
        enter=float(enter_threshold),
        exit=float(enter_threshold) - float(exit_gap),
        active_factor_count=len(selected),
    )


def suggest_trial_parameters(
    trial: optuna.trial.BaseTrial,
    factor_names: Sequence[str],
    origin_weights: pd.Series,
    protocol: Mapping[str, object],
) -> ProjectedStrategy:
    """Ask Optuna for one dense joint strategy and project it deterministically."""
    count_bounds = protocol["active_factor_count"]
    raw_low, raw_high = _bounds(protocol, "raw_weight_bounds")
    enter_low, enter_high = _bounds(protocol, "enter_threshold_bounds")
    gap_low, gap_high = _bounds(protocol, "exit_gap_bounds")
    active_count = trial.suggest_int(
        "active_factor_count", int(count_bounds["low"]), int(count_bounds["high"])
    )
    raw = pd.Series(
        {
            str(name): trial.suggest_float(
                f"raw_weight::{name}", raw_low, raw_high
            )
            for name in factor_names
        },
        dtype=float,
    )
    enter = trial.suggest_float("enter_threshold", enter_low, enter_high)
    gap = trial.suggest_float("exit_gap", gap_low, gap_high)
    return project_trial_parameters(
        factor_names,
        raw,
        active_count,
        enter,
        gap,
        origin_weights,
        protocol,
    )


def trial_params_for_strategy(
    weights: pd.Series,
    enter: float,
    exit_: float,
    protocol: Mapping[str, object],
) -> dict[str, float | int]:
    """Encode a valid frozen strategy so projection reproduces it as Trial 0."""
    values = weights.astype(float)
    active = values[values.ne(0.0)]
    minimum = float(protocol["minimum_absolute_weight"])
    if active.empty or active.abs().lt(minimum - 1e-15).any():
        raise ValueError("frozen strategy contains invalid active weights")
    if abs(float(active.abs().sum()) - 1.0) > 1e-12:
        raise ValueError("frozen strategy weights must have L1 norm one")
    gap = float(enter) - float(exit_)
    params: dict[str, float | int] = {
        "active_factor_count": len(active),
        "enter_threshold": float(enter),
        "exit_gap": gap,
    }
    for name, value in values.items():
        raw = 0.0
        if value != 0.0:
            raw = float(np.sign(value) * (abs(value) - minimum))
        params[f"raw_weight::{name}"] = raw
    return params


def rank_trial_results(rows: pd.DataFrame) -> pd.DataFrame:
    """Apply the preregistered robust return-only trial ordering."""
    required = [
        "trial_number",
        "min_return_delta",
        "win_count",
        "median_return_delta",
        "mean_return_delta",
        "active_factor_count",
    ]
    missing = [column for column in required if column not in rows]
    if missing:
        raise ValueError(f"trial results missing columns {missing}")
    return rows.sort_values(
        [
            "min_return_delta",
            "win_count",
            "median_return_delta",
            "mean_return_delta",
            "active_factor_count",
            "trial_number",
        ],
        ascending=[False, False, False, False, True, True],
        kind="stable",
    ).reset_index(drop=True)


def enqueue_initial_trial(study: optuna.Study, params: Mapping[str, Any]) -> bool:
    """Enqueue the fixed baseline exactly once, before any study trial exists."""
    if study.trials:
        return False
    study.enqueue_trial(dict(params), skip_if_exists=True)
    return True


def recover_running_trials(study: optuna.Study) -> tuple[int, ...]:
    """Mark trials abandoned by an interrupted parent process as failed."""
    running = tuple(
        trial.number
        for trial in study.get_trials(deepcopy=False)
        if trial.state == optuna.trial.TrialState.RUNNING
    )
    for number in running:
        study.tell(number, state=optuna.trial.TrialState.FAIL)
    return running


def _completed_progress(study: optuna.Study) -> tuple[int, int]:
    completed = sorted(
        (
            trial
            for trial in study.get_trials(deepcopy=False)
            if trial.state == optuna.trial.TrialState.COMPLETE
        ),
        key=lambda trial: trial.number,
    )
    best = -np.inf
    since_improvement = 0
    for trial in completed:
        value = float(trial.value)
        if value > best:
            best = value
            since_improvement = 0
        else:
            since_improvement += 1
    return len(completed), since_improvement


def run_study_batches(
    study: optuna.Study,
    request_factory: Callable[[optuna.Trial], TrialRequest],
    batch_evaluator: Callable[[tuple[TrialRequest, ...]], Sequence[TrialOutcome]],
    *,
    maximum_completed_trials: int,
    minimum_completed_trials: int,
    no_improvement_trials: int,
    maximum_wall_time_seconds: float,
    batch_size: int,
    collect_batch_timings: bool = False,
) -> dict[str, object]:
    """Run synchronous batches while keeping every storage write in the parent."""
    if not 0 < minimum_completed_trials <= maximum_completed_trials:
        raise ValueError("completed trial limits are invalid")
    if no_improvement_trials <= 0 or maximum_wall_time_seconds <= 0 or batch_size <= 0:
        raise ValueError("study stopping limits must be positive")
    started = perf_counter()
    stop_reason = "maximum_completed_trials"
    batch_timings: list[dict[str, float | int]] = []
    while True:
        completed, since_improvement = _completed_progress(study)
        elapsed = perf_counter() - started
        if completed >= maximum_completed_trials:
            stop_reason = "maximum_completed_trials"
            break
        if elapsed >= maximum_wall_time_seconds:
            stop_reason = "maximum_wall_time_seconds"
            break
        if completed >= minimum_completed_trials and since_improvement >= no_improvement_trials:
            stop_reason = "no_improvement_trials"
            break

        request_count = min(batch_size, maximum_completed_trials - completed)
        batch_started = perf_counter()
        ask_started = batch_started
        trials: dict[int, optuna.Trial] = {}
        requests: list[TrialRequest] = []
        for _ in range(request_count):
            trial = study.ask()
            request = request_factory(trial)
            if request.number != trial.number or request.number in trials:
                raise RuntimeError("trial request numbers differ from Optuna trials")
            trials[trial.number] = trial
            requests.append(request)
        ask_finished = perf_counter()
        outcomes = tuple(batch_evaluator(tuple(requests)))
        evaluate_finished = perf_counter()
        expected = tuple(sorted(trials))
        actual = tuple(sorted(outcome.number for outcome in outcomes))
        if actual != expected or len({outcome.number for outcome in outcomes}) != len(outcomes):
            raise RuntimeError("trial result numbers are missing, duplicated, or unknown")
        by_number = {outcome.number: outcome for outcome in outcomes}
        for number in expected:
            outcome = by_number[number]
            trial = trials[number]
            for key, value in sorted(outcome.user_attrs.items()):
                trial.set_user_attr(str(key), value)
            if outcome.error is not None or outcome.value is None:
                if outcome.error is not None:
                    trial.set_user_attr("error", str(outcome.error))
                study.tell(trial, state=optuna.trial.TrialState.FAIL)
            else:
                study.tell(trial, float(outcome.value))
        tell_finished = perf_counter()
        if collect_batch_timings:
            batch_timings.append(
                {
                    "batch_number": len(batch_timings),
                    "first_trial_number": expected[0],
                    "trial_count": len(expected),
                    "ask_and_project_seconds": ask_finished - ask_started,
                    "parallel_evaluate_seconds": evaluate_finished - ask_finished,
                    "tell_and_attrs_seconds": tell_finished - evaluate_finished,
                    "batch_total_seconds": tell_finished - batch_started,
                }
            )
    completed, since_improvement = _completed_progress(study)
    result: dict[str, object] = {
        "stop_reason": stop_reason,
        "completed_trials": completed,
        "trials_since_improvement": since_improvement,
        "elapsed_seconds": perf_counter() - started,
    }
    if collect_batch_timings:
        result["batch_timings"] = batch_timings
    return result


def _flat_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def export_study_trials(study: optuna.Study, path: Path) -> pd.DataFrame:
    """Export all runtime study evidence as a stable, Git-friendly table."""
    rows: list[dict[str, Any]] = []
    for trial in sorted(study.get_trials(deepcopy=False), key=lambda item: item.number):
        row: dict[str, Any] = {
            "number": trial.number,
            "state": trial.state.name,
            "value": trial.value,
            "datetime_start": trial.datetime_start.isoformat() if trial.datetime_start else None,
            "datetime_complete": (
                trial.datetime_complete.isoformat() if trial.datetime_complete else None
            ),
            "duration_seconds": (
                trial.duration.total_seconds() if trial.duration is not None else None
            ),
        }
        row.update({f"param::{key}": _flat_value(value) for key, value in trial.params.items()})
        row.update(
            {f"attr::{key}": _flat_value(value) for key, value in trial.user_attrs.items()}
        )
        rows.append(row)
    frame = pd.DataFrame(rows).sort_values("number").reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    return frame
