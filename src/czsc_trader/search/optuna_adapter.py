"""Thin Optuna adapter with frozen search contracts and complete trial ledgers."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Literal, TypeAlias

import numpy as np
import optuna
import pandas as pd
from optuna.samplers import GridSampler, NSGAIISampler, RandomSampler, TPESampler
from optuna.trial import FrozenTrial, Trial, TrialState


Direction = Literal["maximize", "minimize"]
SearchMethod = Literal["grid", "random", "tpe", "nsga2"]


class SearchIntegrationError(ValueError):
    """Invalid search contract or incompatible stored study."""


class SearchTrialRejected(Exception):
    """Expected parameter-combination rejection recorded as a pruned trial."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SearchExecutionError(RuntimeError):
    """Unexpected evaluator failure; completed storage remains available for audit."""


@dataclass(frozen=True)
class FloatParameter:
    name: str
    low: float
    high: float
    step: float | None = None
    log: bool = False

    def suggest(self, trial: Trial) -> float:
        return trial.suggest_float(self.name, self.low, self.high, step=self.step, log=self.log)


@dataclass(frozen=True)
class IntParameter:
    name: str
    low: int
    high: int
    step: int = 1
    log: bool = False

    def suggest(self, trial: Trial) -> int:
        return trial.suggest_int(self.name, self.low, self.high, step=self.step, log=self.log)


@dataclass(frozen=True)
class CategoricalParameter:
    name: str
    choices: tuple[str | int | float | bool, ...]

    def suggest(self, trial: Trial) -> str | int | float | bool:
        return trial.suggest_categorical(self.name, self.choices)


ParameterSpec: TypeAlias = FloatParameter | IntParameter | CategoricalParameter


@dataclass(frozen=True)
class ObjectiveSpec:
    name: str
    direction: Direction


@dataclass(frozen=True)
class ConstraintSpec:
    """A metric is feasible when its value is no greater than upper_bound."""

    name: str
    upper_bound: float


@dataclass(frozen=True)
class SearchSpec:
    study_name: str
    method: SearchMethod
    parameters: tuple[ParameterSpec, ...]
    objectives: tuple[ObjectiveSpec, ...]
    target_trials: int
    seed: int
    storage_path: Path | None = None
    constraints: tuple[ConstraintSpec, ...] = ()
    grid: Mapping[str, Sequence[str | int | float | bool]] | None = None
    workers: int = 1


@dataclass(frozen=True)
class SearchEvaluation:
    objectives: Mapping[str, float]
    constraints: Mapping[str, float] = field(default_factory=dict)
    candidate_id: str = ""
    strategy_hash: str = ""
    behavior_hash: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchResult:
    status: Literal["PASS"]
    outcome: Literal["CANDIDATES_AVAILABLE", "NO_FEASIBLE_TRIALS", "NO_NEW_TRIALS"]
    study_name: str
    method: SearchMethod
    contract_digest: str
    target_trials: int
    existing_trials: int
    created_trials: int
    completed_trials: int
    pruned_trials: int
    failed_trials: int
    feasible_trials: int
    ledger: pd.DataFrame


Evaluator: TypeAlias = Callable[[Mapping[str, object]], SearchEvaluation]


def _parameter_payload(parameter: ParameterSpec) -> dict[str, object]:
    return {"type": type(parameter).__name__, **asdict(parameter)}


def _contract_payload(spec: SearchSpec) -> dict[str, object]:
    return {
        "schema_version": 1,
        "study_name": spec.study_name,
        "method": spec.method,
        "parameters": [_parameter_payload(parameter) for parameter in spec.parameters],
        "objectives": [asdict(objective) for objective in spec.objectives],
        "constraints": [asdict(constraint) for constraint in spec.constraints],
        "seed": spec.seed,
        "grid": None
        if spec.grid is None
        else {name: list(values) for name, values in sorted(spec.grid.items())},
    }


def _contract_digest(spec: SearchSpec) -> str:
    raw = json.dumps(
        _contract_payload(spec), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_parameter(parameter: ParameterSpec) -> None:
    if not parameter.name or parameter.name.startswith("__"):
        raise SearchIntegrationError("parameter names must be non-empty and not reserved")
    if isinstance(parameter, FloatParameter):
        if not np.isfinite([parameter.low, parameter.high]).all() or parameter.low >= parameter.high:
            raise SearchIntegrationError(f"invalid float range: {parameter.name}")
        if parameter.step is not None and parameter.step <= 0:
            raise SearchIntegrationError(f"float step must be positive: {parameter.name}")
        if parameter.log and (parameter.low <= 0 or parameter.step is not None):
            raise SearchIntegrationError(f"log float cannot use nonpositive bounds or step: {parameter.name}")
    elif isinstance(parameter, IntParameter):
        if parameter.low >= parameter.high or parameter.step <= 0:
            raise SearchIntegrationError(f"invalid integer range: {parameter.name}")
        if parameter.log and (parameter.low <= 0 or parameter.step != 1):
            raise SearchIntegrationError(f"log integer requires positive bounds and step 1: {parameter.name}")
    else:
        try:
            unique_choices = set(parameter.choices)
        except TypeError as exc:
            raise SearchIntegrationError(
                f"categorical choices must be scalar values: {parameter.name}"
            ) from exc
        if not parameter.choices or len(unique_choices) != len(parameter.choices):
            raise SearchIntegrationError(
                f"categorical choices must be non-empty and unique: {parameter.name}"
            )


def _validate_grid_value(parameter: ParameterSpec, value: object) -> None:
    if isinstance(parameter, FloatParameter):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SearchIntegrationError(f"grid value must be numeric: {parameter.name}")
        numeric = float(value)
        if not np.isfinite(numeric) or not parameter.low <= numeric <= parameter.high:
            raise SearchIntegrationError(f"grid value is outside float range: {parameter.name}")
        if parameter.step is not None:
            steps = (numeric - parameter.low) / parameter.step
            if not np.isclose(steps, round(steps)):
                raise SearchIntegrationError(f"grid value does not match float step: {parameter.name}")
    elif isinstance(parameter, IntParameter):
        if isinstance(value, bool) or not isinstance(value, int):
            raise SearchIntegrationError(f"grid value must be an integer: {parameter.name}")
        if not parameter.low <= value <= parameter.high or (value - parameter.low) % parameter.step:
            raise SearchIntegrationError(f"grid value does not match integer range: {parameter.name}")
    elif value not in parameter.choices:
        raise SearchIntegrationError(f"grid value is not a categorical choice: {parameter.name}")


def _validate_spec(spec: SearchSpec) -> None:
    if not spec.study_name or any(char in spec.study_name for char in ("/", "\\")):
        raise SearchIntegrationError("study_name must be a non-empty path-free name")
    if spec.method not in {"grid", "random", "tpe", "nsga2"}:
        raise SearchIntegrationError(f"unsupported search method: {spec.method}")
    if not spec.parameters or not spec.objectives:
        raise SearchIntegrationError("parameters and objectives must be non-empty")
    if spec.target_trials < 1 or spec.workers < 1:
        raise SearchIntegrationError("target_trials and workers must be positive")
    names = [parameter.name for parameter in spec.parameters]
    objective_names = [objective.name for objective in spec.objectives]
    constraint_names = [constraint.name for constraint in spec.constraints]
    if len(names) != len(set(names)) or len(objective_names) != len(set(objective_names)):
        raise SearchIntegrationError("parameter and objective names must be unique")
    if len(constraint_names) != len(set(constraint_names)):
        raise SearchIntegrationError("constraint names must be unique")
    if set(objective_names).intersection(constraint_names):
        raise SearchIntegrationError("objective and constraint names must not overlap")
    for parameter in spec.parameters:
        _validate_parameter(parameter)
    for objective in spec.objectives:
        if not objective.name or objective.direction not in {"maximize", "minimize"}:
            raise SearchIntegrationError(f"invalid objective: {objective.name}")
    if any(not item.name or not np.isfinite(item.upper_bound) for item in spec.constraints):
        raise SearchIntegrationError("constraints require names and finite upper bounds")
    if spec.method == "grid":
        if spec.grid is None or set(spec.grid) != set(names):
            raise SearchIntegrationError("grid method requires values for every parameter")
        if any(not values for values in spec.grid.values()):
            raise SearchIntegrationError("grid values must be non-empty")
        for parameter in spec.parameters:
            for value in spec.grid[parameter.name]:
                _validate_grid_value(parameter, value)
        combinations = int(np.prod([len(spec.grid[name]) for name in names]))
        if spec.target_trials > combinations:
            raise SearchIntegrationError(
                f"target_trials exceeds grid combinations: target={spec.target_trials}, grid={combinations}"
            )
    elif spec.grid is not None:
        raise SearchIntegrationError("grid values are only valid for grid search")


def _constraints_from_trial(trial: FrozenTrial) -> Sequence[float]:
    return list(trial.user_attrs.get("constraint_violations", ()))


def _sampler(spec: SearchSpec) -> optuna.samplers.BaseSampler:
    constraints = _constraints_from_trial if spec.constraints else None
    if spec.method == "grid":
        return GridSampler(dict(spec.grid or {}), seed=spec.seed)
    if spec.method == "random":
        return RandomSampler(seed=spec.seed)
    if spec.method == "tpe":
        return TPESampler(seed=spec.seed, constraints_func=constraints)
    return NSGAIISampler(seed=spec.seed, constraints_func=constraints)


def _storage_url(path: Path | None) -> str | None:
    if path is None:
        return None
    resolved = Path(path).resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{resolved.as_posix()}"


def _validated_evaluation(spec: SearchSpec, value: SearchEvaluation) -> tuple[float, ...]:
    objectives = dict(value.objectives)
    constraints = dict(value.constraints)
    expected_objectives = {item.name for item in spec.objectives}
    expected_constraints = {item.name for item in spec.constraints}
    if set(objectives) != expected_objectives:
        raise SearchIntegrationError(
            f"objective names differ: expected={sorted(expected_objectives)}, actual={sorted(objectives)}"
        )
    if set(constraints) != expected_constraints:
        raise SearchIntegrationError(
            f"constraint names differ: expected={sorted(expected_constraints)}, actual={sorted(constraints)}"
        )
    objective_values = tuple(float(objectives[item.name]) for item in spec.objectives)
    constraint_values = tuple(float(constraints[item.name]) for item in spec.constraints)
    if not np.isfinite((*objective_values, *constraint_values)).all():
        raise SearchIntegrationError("objective and constraint values must be finite")
    try:
        json.dumps(dict(value.metadata), ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise SearchIntegrationError("evaluation metadata must be JSON serializable") from exc
    return objective_values


def _objective(spec: SearchSpec, evaluator: Evaluator) -> Callable[[Trial], float | tuple[float, ...]]:
    def objective(trial: Trial) -> float | tuple[float, ...]:
        params = {parameter.name: parameter.suggest(trial) for parameter in spec.parameters}
        try:
            evaluation = evaluator(params)
        except SearchTrialRejected as exc:
            trial.set_user_attr("rejection_code", exc.code)
            trial.set_user_attr("rejection_message", exc.message)
            raise optuna.TrialPruned(exc.message) from exc
        values = _validated_evaluation(spec, evaluation)
        constraint_values = dict(evaluation.constraints)
        violations = [
            constraint_values[item.name] - item.upper_bound for item in spec.constraints
        ]
        trial.set_user_attr("constraint_violations", violations)
        trial.set_user_attr("feasible", all(value <= 0 for value in violations))
        trial.set_user_attr("candidate_id", evaluation.candidate_id)
        trial.set_user_attr("strategy_hash", evaluation.strategy_hash)
        trial.set_user_attr("behavior_hash", evaluation.behavior_hash)
        trial.set_user_attr("evaluation_metadata", dict(evaluation.metadata))
        return values[0] if len(values) == 1 else values

    return objective


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _ledger(study: optuna.Study, spec: SearchSpec) -> pd.DataFrame:
    rows = []
    for trial in study.trials:
        values = list(trial.values or ())
        row: dict[str, object] = {
            "trial_number": trial.number,
            "trial_id": f"{spec.study_name}-{trial.number:05d}",
            "state": trial.state.name,
            "params": json.dumps(trial.params, ensure_ascii=False, sort_keys=True),
            "feasible": trial.user_attrs.get("feasible"),
            "candidate_id": trial.user_attrs.get("candidate_id", ""),
            "strategy_hash": trial.user_attrs.get("strategy_hash", ""),
            "behavior_hash": trial.user_attrs.get("behavior_hash", ""),
            "metadata": json.dumps(
                trial.user_attrs.get("evaluation_metadata", {}),
                ensure_ascii=False,
                sort_keys=True,
            ),
            "rejection_code": trial.user_attrs.get("rejection_code", ""),
            "rejection_message": trial.user_attrs.get("rejection_message", ""),
            "datetime_start_utc": _iso(trial.datetime_start),
            "datetime_complete_utc": _iso(trial.datetime_complete),
            "duration_seconds": None if trial.duration is None else trial.duration.total_seconds(),
        }
        for index, objective in enumerate(spec.objectives):
            row[f"objective.{objective.name}"] = values[index] if index < len(values) else None
        violations = list(trial.user_attrs.get("constraint_violations", ()))
        for index, constraint in enumerate(spec.constraints):
            row[f"constraint_violation.{constraint.name}"] = (
                violations[index] if index < len(violations) else None
            )
        rows.append(row)
    return pd.DataFrame(rows).sort_values("trial_number").reset_index(drop=True)


def run_search(spec: SearchSpec, evaluator: Evaluator) -> SearchResult:
    """Run one study, or resume it when persistent SQLite storage is configured."""

    _validate_spec(spec)
    digest = _contract_digest(spec)
    study = optuna.create_study(
        study_name=spec.study_name,
        directions=[item.direction for item in spec.objectives],
        sampler=_sampler(spec),
        storage=_storage_url(spec.storage_path),
        load_if_exists=True,
    )
    stored_digest = study.user_attrs.get("search_contract_digest")
    if stored_digest is None:
        study.set_user_attr("search_contract_digest", digest)
        study.set_user_attr("search_contract", _contract_payload(spec))
    elif stored_digest != digest:
        raise SearchIntegrationError("stored study uses a different search contract")

    existing = len(study.trials)
    remaining = max(0, spec.target_trials - existing)
    if remaining:
        try:
            study.optimize(
                _objective(spec, evaluator),
                n_trials=remaining,
                n_jobs=spec.workers,
                catch=(),
                show_progress_bar=False,
            )
        except Exception as exc:
            raise SearchExecutionError(
                f"search stopped after evaluator failure; study={spec.study_name}, error={exc}"
            ) from exc

    ledger = _ledger(study, spec)
    states = ledger["state"].value_counts().to_dict()
    failed = int(states.get(TrialState.FAIL.name, 0))
    if failed:
        raise SearchExecutionError(
            f"stored study contains failed trials; study={spec.study_name}, failed={failed}"
        )
    completed = int(states.get(TrialState.COMPLETE.name, 0))
    pruned = int(states.get(TrialState.PRUNED.name, 0))
    feasible = int(
        ledger.loc[ledger["state"].eq(TrialState.COMPLETE.name), "feasible"].fillna(False).sum()
    )
    if completed == 0:
        raise SearchExecutionError(f"search produced no completed trials: {spec.study_name}")
    created = len(ledger) - existing
    outcome = (
        "NO_FEASIBLE_TRIALS"
        if feasible == 0
        else "NO_NEW_TRIALS"
        if created == 0
        else "CANDIDATES_AVAILABLE"
    )
    return SearchResult(
        status="PASS",
        outcome=outcome,
        study_name=spec.study_name,
        method=spec.method,
        contract_digest=digest,
        target_trials=spec.target_trials,
        existing_trials=existing,
        created_trials=created,
        completed_trials=completed,
        pruned_trials=pruned,
        failed_trials=failed,
        feasible_trials=feasible,
        ledger=ledger,
    )
