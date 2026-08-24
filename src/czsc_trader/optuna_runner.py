"""Causal EX07 Optuna joint strategy search and formal holdout runner."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import numpy as np
import optuna
import pandas as pd
from joblib import Parallel, delayed, parallel_config

from .audit import audit_no_lookahead
from .backtest import run_period_backtests
from .baselines import resolve_baseline
from .data import MarketData, load_market_data
from .experiment_archive import build_experiment_manifest, validate_experiment_archive
from .factor_discovery import (
    CandidateFactors,
    generate_candidate_factors,
    validate_sparse_weights,
)
from .factor_discovery_runner import (
    VALIDATION_WINDOWS,
    _candidate_rows,
    _metric_view,
    _target,
)
from .factors import generate_factor_frame
from .four_layer import normalized_signal_factors
from .four_layer_runner import _events, _metrics, _target_digest, _write_json
from .objectives import TARGET_PERIODS
from .optuna_search import (
    ProjectedStrategy,
    TrialOutcome,
    TrialRequest,
    enqueue_initial_trial,
    export_study_trials,
    rank_trial_results,
    recover_running_trials,
    run_study_batches,
    suggest_trial_parameters,
    trial_params_for_strategy,
    validate_optuna_protocol,
)
from .return_only_runner import half_year_periods
from .rules import FACTOR_COLUMNS, positions_for_rule


@dataclass(frozen=True)
class SelectionContext:
    """All immutable inputs visible to EX07 before the strategy is frozen."""

    data: MarketData
    candidate: CandidateFactors
    protocol: dict[str, object]
    ex06_protocol: dict[str, object]
    ex04: dict[str, object]
    ex06: dict[str, object]
    baseline: object
    origin_weights: pd.Series
    periods: dict[str, tuple[pd.Timestamp, pd.Timestamp]]
    baseline_metrics: dict[str, dict[str, object]]
    identity: dict[str, object]


@dataclass(frozen=True)
class TrialEvaluationInputs:
    """Read-only values shared by independent trial workers."""

    factors: pd.DataFrame
    daily: pd.DataFrame
    state_rule: object
    periods: dict[str, tuple[pd.Timestamp, pd.Timestamp]]
    baseline_metrics: dict[str, dict[str, object]]
    fee_rate: float
    init_cash: float

    @classmethod
    def from_context(
        cls,
        context: SelectionContext,
        *,
        fee_rate: float = 0.0005,
        init_cash: float = 1_000_000.0,
    ) -> "TrialEvaluationInputs":
        return cls(
            factors=context.candidate.factors,
            daily=context.data.daily,
            state_rule=context.baseline.rule,
            periods=context.periods,
            baseline_metrics=context.baseline_metrics,
            fee_rate=float(fee_rate),
            init_cash=float(init_cash),
        )


def validate_source_hash(path: Path, expected: str, label: str) -> str:
    """Verify one immutable input before it influences the experiment."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"missing {label}: {path}")
    actual = sha256(path.read_bytes()).hexdigest()
    if actual != str(expected).lower():
        raise ValueError(f"{label} hash differs from preregistration")
    return actual


def validate_ex07_protocol(protocol: Mapping[str, object]) -> None:
    """Validate the complete formal EX07 protocol."""
    validate_optuna_protocol(protocol)
    if tuple(protocol.get("validation_windows", ())) != VALIDATION_WINDOWS:
        raise ValueError("validation windows differ from preregistration")
    if tuple(protocol.get("holdout_windows", ())) != tuple(TARGET_PERIODS):
        raise ValueError("holdout windows differ from preregistration")
    source = protocol.get("candidate_source")
    if not isinstance(source, Mapping) or source.get("candidate_factor_count") != 91:
        raise ValueError("candidate source differs from preregistration")
    if protocol.get("runtime_database_tracked") is not False:
        raise ValueError("runtime database must not be tracked")
    if protocol.get("staged_search") is not False:
        raise ValueError("staged search is forbidden")


def validate_ex08_protocol(protocol: Mapping[str, object]) -> None:
    """Require the exact preregistered single-variable EX08 contract."""
    expected_top = {
        "schema_version": 1,
        "experiment_id": "0824_EX08",
        "experiment_type": "optuna_inmemory_full_joint_strategy_search",
        "status": "PRE_REGISTERED",
        "symbol": "588080.SH",
        "selection_sample_end": "2025-12-31",
        "selection_metric": "minimum_return_delta_vs_ex04",
        "pass_rule": "strictly higher return than EX04 in every 2026 holdout window",
        "holdout_access_before_freeze": False,
        "storage_mode": "memory",
        "runtime_database": None,
        "runtime_database_tracked": False,
        "staged_search": False,
    }
    expected_baseline = {
        "experiment": "0824_EX04",
        "file": "experiments/0824_EX04/artifacts/frozen_challenger.json",
        "sha256": "c6fa86c0f87743564231dec4fb6e66971bde0fa4107829d077cb5cf31c53885f",
    }
    expected_source = {
        "experiment": "0824_EX06",
        "protocol_sha256": "e47b31a21d5b309553ec2819878cabaabb0b7f55e54a5a03e0def9c25666649f",
        "factor_candidates_sha256": "a27eec780e9b49951200467468853ebdc0ac160de1fda6b6c9a50e4e0625e4eb",
        "factor_universe_sha256": "7ac73a46db6210f9b2b79d16780a9985cdd3a509b229217d71e10f7cdb408f9f",
        "frozen_challenger_sha256": "868e989439e4442b4637f7976f438705e61b1d023f688ab7ce9d2ee1d0ac7b89",
        "candidate_names_sha256": "19d0c0d9eabf9147f1f84c68ba8a24eb061b4b93db12d22a0e1b1e86567252b9",
        "candidate_factor_count": 91,
    }
    expected_optuna = {
        "version": "4.9.0",
        "sampler": "TPESampler",
        "seed": 20260824,
        "n_startup_trials": 256,
        "multivariate": True,
        "group": False,
        "constant_liar": True,
        "maximum_completed_trials": 4096,
        "minimum_completed_trials": 4096,
        "no_improvement_trials": None,
        "maximum_wall_time_seconds": None,
        "batch_size": 8,
    }
    expected_parallel = {
        "backend": "loky",
        "n_jobs": 8,
        "inner_max_num_threads": 1,
        "database_writer": "none",
        "result_order": "trial_number",
    }
    expected_projection = {
        "active_factor_count": {"low": 6, "high": 18},
        "raw_weight_bounds": [-1.0, 1.0],
        "minimum_absolute_weight": 0.0125,
        "minimum_trend_weight": 0.1,
        "protect_volume_window": True,
        "enter_threshold_bounds": [0.05, 0.3],
        "exit_gap_bounds": [0.025, 0.25],
    }
    actual_top = {key: protocol.get(key) for key in expected_top}
    actual_projection = {key: protocol.get(key) for key in expected_projection}
    if (
        actual_top != expected_top
        or protocol.get("research_baseline") != expected_baseline
        or protocol.get("candidate_source") != expected_source
        or tuple(protocol.get("validation_windows", ())) != VALIDATION_WINDOWS
        or tuple(protocol.get("holdout_windows", ())) != tuple(TARGET_PERIODS)
        or protocol.get("optuna") != expected_optuna
        or protocol.get("parallel") != expected_parallel
        or actual_projection != expected_projection
    ):
        raise ValueError("EX08 protocol differs from preregistration")


def validate_candidate_identity(
    actual_names: Sequence[str], source_csv: Path, *, expected_count: int
) -> dict[str, object]:
    """Require exact equality with the frozen EX06 candidate names and order."""
    source_csv = Path(source_csv)
    frame = pd.read_csv(source_csv, encoding="utf-8-sig")
    if "factor" not in frame:
        raise ValueError("candidate source has no factor column")
    expected = tuple(map(str, frame["factor"].tolist()))
    actual = tuple(map(str, actual_names))
    if len(expected) != expected_count or actual != expected:
        raise ValueError("candidate identity or order differs from EX06")
    names_payload = json.dumps(actual, ensure_ascii=False, separators=(",", ":"))
    return {
        "status": "PASS",
        "candidate_factor_count": len(actual),
        "names_sha256": sha256(names_payload.encode("utf-8")).hexdigest(),
        "source_csv_sha256": sha256(source_csv.read_bytes()).hexdigest(),
    }


def build_trial_outcome(
    number: int,
    strategy: ProjectedStrategy,
    challenger: Mapping[str, Mapping[str, object]],
    baseline: Mapping[str, Mapping[str, object]],
    target_digest: str,
) -> TrialOutcome:
    """Convert full-window metrics into the return-only Optuna objective."""
    if tuple(challenger) != tuple(baseline):
        raise ValueError("challenger windows differ from baseline")
    deltas = {
        name: float(challenger[name]["strategy_return"])
        - float(baseline[name]["strategy_return"])
        for name in baseline
    }
    values = list(deltas.values())
    attrs = {
        "target_digest": str(target_digest),
        "active_factor_count": int(strategy.active_factor_count),
        "enter": float(strategy.enter),
        "exit": float(strategy.exit),
        "win_count": int(sum(delta > 0.0 for delta in values)),
        "min_return_delta": float(min(values)),
        "median_return_delta": float(np.median(values)),
        "mean_return_delta": float(np.mean(values)),
        "window_return_deltas": deltas,
        "window_metrics": {
            name: {key: value for key, value in metrics.items()}
            for name, metrics in challenger.items()
        },
    }
    return TrialOutcome(number=number, value=float(min(values)), user_attrs=attrs)


def evaluate_trial_request(
    request: TrialRequest, inputs: TrialEvaluationInputs
) -> TrialOutcome:
    """Evaluate one projected strategy without reading or writing experiment files."""
    if not isinstance(request.payload, ProjectedStrategy):
        raise TypeError("trial request payload must be a ProjectedStrategy")
    strategy = request.payload
    target, _ = _target(
        inputs.factors,
        strategy.weights,
        strategy.enter,
        strategy.exit,
        inputs.state_rule,
    )
    challenger = _metrics(
        run_period_backtests(
            inputs.daily,
            target,
            inputs.periods,
            fee_rate=inputs.fee_rate,
            init_cash=inputs.init_cash,
        )
    )
    return build_trial_outcome(
        request.number,
        strategy,
        challenger,
        inputs.baseline_metrics,
        _target_digest(target),
    )


def evaluate_trial_batch(
    requests: tuple[TrialRequest, ...],
    inputs: TrialEvaluationInputs,
    *,
    n_jobs: int,
) -> tuple[TrialOutcome, ...]:
    """Evaluate a synchronous trial batch with no worker-side file writes."""
    if n_jobs not in {1, 2, 4, 8}:
        raise ValueError("n_jobs must be one of 1, 2, 4, 8")
    if len({request.number for request in requests}) != len(requests):
        raise ValueError("trial request numbers must be unique")
    with parallel_config(
        backend="loky",
        inner_max_num_threads=1,
        max_nbytes="100K",
        mmap_mode="r",
    ):
        outcomes = Parallel(n_jobs=n_jobs)(
            delayed(evaluate_trial_request)(request, inputs) for request in requests
        )
    return tuple(sorted(outcomes, key=lambda outcome: outcome.number))


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return payload


def prepare_selection_inputs(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    *,
    fee_rate: float = 0.0005,
    init_cash: float = 1_000_000.0,
    protocol_validator: Callable[[Mapping[str, object]], None] = validate_ex07_protocol,
) -> SelectionContext:
    """Load and validate only the information allowed before EX07 freezes."""
    experiment_dir = Path(experiment_dir).resolve()
    repo_root = experiment_dir.parents[1]
    protocol = _read_json(experiment_dir / "artifacts" / "protocol.json")
    protocol_validator(protocol)
    source = protocol["candidate_source"]
    baseline_spec = protocol["research_baseline"]

    ex04_path = repo_root / str(baseline_spec["file"])
    ex06_protocol_path = repo_root / "experiments/0824_EX06/artifacts/protocol.json"
    ex06_candidates_path = repo_root / "experiments/0824_EX06/artifacts/factor_candidates.csv"
    ex06_universe_path = repo_root / "experiments/0824_EX06/artifacts/factor_universe.json"
    ex06_frozen_path = repo_root / "experiments/0824_EX06/artifacts/frozen_challenger.json"
    validate_source_hash(ex04_path, str(baseline_spec["sha256"]), "EX04 baseline")
    validate_source_hash(
        ex06_protocol_path, str(source["protocol_sha256"]), "EX06 protocol"
    )
    validate_source_hash(
        ex06_candidates_path,
        str(source["factor_candidates_sha256"]),
        "EX06 factor candidates",
    )
    validate_source_hash(
        ex06_universe_path,
        str(source["factor_universe_sha256"]),
        "EX06 factor universe",
    )
    validate_source_hash(
        ex06_frozen_path,
        str(source["frozen_challenger_sha256"]),
        "EX06 frozen challenger",
    )
    ex04 = _read_json(ex04_path)
    ex06_protocol = _read_json(ex06_protocol_path)
    ex06 = _read_json(ex06_frozen_path)
    legacy = ex06_protocol["legacy_champion"]
    baseline = resolve_baseline(Path(baseline_root), str(legacy["version"]))
    if baseline.sha256 != str(legacy["sha256"]):
        raise ValueError("legacy champion hash differs from EX06 protocol")

    data = load_market_data(
        raw_dir,
        str(protocol["symbol"]),
        "etf",
        cutoff=str(protocol["selection_sample_end"]),
    )
    ex04_weights = pd.Series(ex04["weights"], dtype=float)
    candidate = generate_candidate_factors(data, ex06_protocol, ex04_weights)
    identity = validate_candidate_identity(
        tuple(candidate.factors.columns),
        ex06_candidates_path,
        expected_count=int(source["candidate_factor_count"]),
    )
    origin = ex04_weights.reindex(candidate.factors.columns).fillna(0.0)
    ex04_target, _ = _target(
        candidate.factors,
        origin,
        float(ex04["spec"]["enter"]),
        float(ex04["spec"]["exit"]),
        baseline.rule,
    )
    all_periods = half_year_periods(2021, 2025)
    periods = {name: all_periods[name] for name in VALIDATION_WINDOWS}
    baseline_metrics = _metrics(
        run_period_backtests(
            data.daily,
            ex04_target,
            periods,
            fee_rate=fee_rate,
            init_cash=init_cash,
        )
    )
    return SelectionContext(
        data=data,
        candidate=candidate,
        protocol=protocol,
        ex06_protocol=ex06_protocol,
        ex04=ex04,
        ex06=ex06,
        baseline=baseline,
        origin_weights=origin,
        periods=periods,
        baseline_metrics=baseline_metrics,
        identity=identity,
    )


def _study_from_protocol(
    runtime_dir: Path,
    protocol: Mapping[str, object],
    protocol_digest: str,
    *,
    storage_mode: str = "sqlite",
) -> optuna.Study:
    config = protocol["optuna"]
    if storage_mode == "sqlite":
        runtime_dir.mkdir(parents=True, exist_ok=True)
        database = runtime_dir / "optuna.db"
        storage: str | optuna.storages.BaseStorage = f"sqlite:///{database.as_posix()}"
    elif storage_mode == "memory":
        storage = optuna.storages.InMemoryStorage()
    else:
        raise ValueError(f"unsupported storage mode: {storage_mode}")
    sampler = optuna.samplers.TPESampler(
        seed=int(config["seed"]),
        n_startup_trials=int(config["n_startup_trials"]),
        multivariate=bool(config["multivariate"]),
        group=bool(config["group"]),
        constant_liar=bool(config["constant_liar"]),
    )
    study = optuna.create_study(
        study_name=str(protocol["experiment_id"]),
        direction="maximize",
        sampler=sampler,
        storage=storage,
        load_if_exists=storage_mode == "sqlite",
    )
    expected_attrs = {
        "experiment_id": str(protocol["experiment_id"]),
        "protocol_sha256": protocol_digest,
        "selection_sample_end": str(protocol["selection_sample_end"]),
    }
    for key, expected in expected_attrs.items():
        actual = study.user_attrs.get(key)
        if actual is not None and actual != expected:
            raise ValueError(f"runtime study {key} differs from formal protocol")
        study.set_user_attr(key, expected)
    return study


def _completed_trial_rows(study: optuna.Study) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for trial in study.get_trials(deepcopy=False):
        if trial.state != optuna.trial.TrialState.COMPLETE:
            continue
        attrs = trial.user_attrs
        rows.append(
            {
                "trial_number": trial.number,
                "min_return_delta": float(attrs["min_return_delta"]),
                "win_count": int(attrs["win_count"]),
                "median_return_delta": float(attrs["median_return_delta"]),
                "mean_return_delta": float(attrs["mean_return_delta"]),
                "active_factor_count": int(attrs["active_factor_count"]),
            }
        )
    if not rows:
        raise ValueError("Optuna study has no completed trials")
    return rank_trial_results(pd.DataFrame(rows))


def _freeze_winner(
    study: optuna.Study,
    context: SelectionContext,
    artifacts: Path,
    evaluation_inputs: TrialEvaluationInputs,
) -> tuple[dict[str, object], str, TrialOutcome]:
    ranked = _completed_trial_rows(study)
    best_number = int(ranked.iloc[0]["trial_number"])
    best_trial = study.trials[best_number]
    strategy = suggest_trial_parameters(
        optuna.trial.FixedTrial(best_trial.params),
        tuple(context.candidate.factors.columns),
        context.origin_weights,
        context.protocol,
    )
    verified = evaluate_trial_request(TrialRequest(best_number, strategy), evaluation_inputs)
    if abs(float(verified.value) - float(best_trial.value)) > 1e-12:
        raise AssertionError("uncached winning objective differs from Optuna trial")
    active = strategy.weights[strategy.weights.ne(0.0)]
    weights_frame = strategy.weights.rename_axis("factor").reset_index()
    weights_frame["active"] = weights_frame["weight"].ne(0.0)
    weights_frame.to_csv(artifacts / "factor_weights.csv", index=False, encoding="utf-8-sig")
    frozen = {
        "schema_version": 1,
        "experiment": str(context.protocol["experiment_id"]),
        "sample_end": str(context.protocol["selection_sample_end"]),
        "research_baseline": context.protocol["research_baseline"],
        "candidate_source": context.protocol["candidate_source"],
        "factor_policy": "fixed_ex06_universe_optuna_joint_subset_weight_threshold_search",
        "trial_number": best_number,
        "factor_names": list(active.index),
        "weights": active.to_dict(),
        "enter": strategy.enter,
        "exit": strategy.exit,
        "selection_metric": str(context.protocol["selection_metric"]),
        "selection_objective": float(verified.value),
    }
    path = artifacts / "frozen_challenger.json"
    _write_json(path, frozen)
    return frozen, sha256(path.read_bytes()).hexdigest(), verified


def _holdout_window(
    ex04: Mapping[str, object],
    ex06: Mapping[str, object],
    legacy: Mapping[str, object],
    buyhold: Mapping[str, object],
    challenger: Mapping[str, object],
) -> dict[str, object]:
    ex04_return = float(ex04["strategy_return"])
    challenger_return = float(challenger["strategy_return"])
    return {
        "ex04": _metric_view(ex04),
        "ex06": _metric_view(ex06),
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


def _run_holdout(
    raw_dir: Path,
    context: SelectionContext,
    frozen: Mapping[str, object],
    frozen_digest: str,
    artifacts: Path,
    *,
    fee_rate: float,
    init_cash: float,
) -> dict[str, object]:
    cutoff = max(end for _, end in TARGET_PERIODS.values())
    data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=cutoff)
    ex04_weights = pd.Series(context.ex04["weights"], dtype=float)
    ex06_weights = pd.Series(context.ex06["weights"], dtype=float)
    challenger_weights = pd.Series(frozen["weights"], dtype=float)
    replay_names = list(dict.fromkeys([*challenger_weights.index, *ex06_weights.index]))
    candidate = generate_candidate_factors(
        data,
        context.ex06_protocol,
        ex04_weights,
        frozen_names=replay_names,
    )
    validation_protocol = dict(context.ex06_protocol)
    validation_protocol["maximum_active_factors"] = 18
    validate_sparse_weights(
        challenger_weights, challenger_weights.index, validation_protocol
    )
    challenger_target, challenger_scores = _target(
        candidate.factors.loc[:, list(challenger_weights.index)],
        challenger_weights,
        float(frozen["enter"]),
        float(frozen["exit"]),
        context.baseline.rule,
    )
    ex06_target, _ = _target(
        candidate.factors.loc[:, list(ex06_weights.index)],
        ex06_weights,
        float(context.ex06["enter"]),
        float(context.ex06["exit"]),
        context.baseline.rule,
    )
    legacy_frame = generate_factor_frame(data).frame
    existing = normalized_signal_factors(legacy_frame.filter(like="raw__"))
    ex04_target, _ = _target(
        existing,
        ex04_weights,
        float(context.ex04["spec"]["enter"]),
        float(context.ex04["spec"]["exit"]),
        context.baseline.rule,
    )
    legacy_target, _ = positions_for_rule(
        legacy_frame.loc[:, FACTOR_COLUMNS], context.baseline.rule
    )
    buyhold_target = pd.Series(1.0, index=challenger_target.index, name="target_position")

    def metrics_for(
        target: pd.Series, **kwargs: Any
    ) -> tuple[dict[str, dict[str, object]], Mapping[str, object]]:
        results = run_period_backtests(
            data.daily,
            target,
            TARGET_PERIODS,
            fee_rate=fee_rate,
            init_cash=init_cash,
            **kwargs,
        )
        return _metrics(results), results

    ex04_metrics, _ = metrics_for(ex04_target)
    ex06_metrics, _ = metrics_for(ex06_target)
    legacy_metrics, _ = metrics_for(legacy_target)
    buyhold_metrics, _ = metrics_for(buyhold_target)
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
    challenger_metrics, challenger_results = metrics_for(
        challenger_target,
        factor_events=events,
        factor_frame=audit_frame,
    )
    windows = {
        name: _holdout_window(
            ex04_metrics[name],
            ex06_metrics[name],
            legacy_metrics[name],
            buyhold_metrics[name],
            challenger_metrics[name],
        )
        for name in TARGET_PERIODS
    }
    overall = all(bool(row["pass"]) for row in windows.values())
    orders = pd.concat(
        [result.orders for result in challenger_results.values()], ignore_index=True
    )
    used_events = pd.concat(
        [events, *[result.factor_events for result in challenger_results.values()]],
        ignore_index=True,
    ).drop_duplicates(subset=["event_id"])
    audit = audit_no_lookahead(orders, used_events, challenger_target, audit_frame)
    orders.to_csv(artifacts / "orders.csv", index=False, encoding="utf-8-sig")
    _write_json(artifacts / "causal_audit.json", audit)
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


def _write_research_docs(
    experiment_dir: Path,
    study_summary: Mapping[str, object],
    frozen: Mapping[str, object],
    holdout: Mapping[str, object],
) -> None:
    experiment_id = str(frozen["experiment"])
    experiment_label = experiment_id.split("_")[-1]
    (experiment_dir / "03_execution.md").write_text(
        "# 执行过程\n\n"
        f"- 状态：正式执行完成（`{holdout['status']}`）\n"
        f"- 完成Trial：{study_summary['completed_trials']}项\n"
        f"- 失败Trial：{study_summary['failed_trials']}项\n"
        f"- 停止原因：`{study_summary['stop_reason']}`\n"
        f"- 搜索耗时：{float(study_summary['elapsed_seconds']):.2f}秒\n"
        f"- 最佳Trial：{frozen['trial_number']}\n"
        f"- 样本内最差收益增量：{float(frozen['selection_objective']):+.4%}\n"
        f"- 最终非零因子：{len(frozen['factor_names'])}项\n"
        "- 选优指标：仅收益率\n"
        "- 冻结前访问2026：否\n"
        "- 冻结后访问2026：是\n"
        f"- 因果审计：`{holdout['audit']['status']}`\n",
        encoding="utf-8",
    )
    lines = [
        "# 研究结论",
        "",
        f"{experiment_label} Optuna联合搜索结果为 **{holdout['status']}**。",
        "",
        f"| 窗口 | EX04收益 | {experiment_label}收益 | 收益增量 | EX04夏普 | {experiment_label}夏普 | 判定 |",
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
            f"- 最佳Trial：{frozen['trial_number']}。",
            f"- 非零因子：{len(frozen['factor_names'])}项。",
            f"- 入场阈值：{float(frozen['enter']):.6f}；离场阈值：{float(frozen['exit']):.6f}。",
            "- 夏普率只报告，未参与选优或PASS。",
            "- EX06、旧冠军与Buy & Hold完整指标见`artifacts/holdout_metrics.json`。",
            "- 2026结果未触发二次调参。",
        ]
    )
    (experiment_dir / "04_conclusion.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def run_optuna_experiment(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    *,
    fee_rate: float = 0.0005,
    init_cash: float = 1_000_000.0,
    protocol_validator: Callable[[Mapping[str, object]], None] = validate_ex07_protocol,
    storage_mode: str = "sqlite",
    recover_runtime: bool = True,
    collect_batch_timings: bool = False,
    require_full_trial_count: bool = False,
    execution_commit: str | None = None,
) -> dict[str, object]:
    """Run a validated Optuna study, freeze its winner, then unlock holdout."""
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol_digest = sha256(protocol_path.read_bytes()).hexdigest()
    context = prepare_selection_inputs(
        raw_dir,
        baseline_root,
        experiment_dir,
        fee_rate=fee_rate,
        init_cash=init_cash,
        protocol_validator=protocol_validator,
    )
    _write_json(artifacts / "candidate_identity.json", context.identity)
    _candidate_rows(context.candidate).to_csv(
        artifacts / "factor_candidates.csv", index=False, encoding="utf-8-sig"
    )
    _write_json(artifacts / "factor_universe.json", context.candidate.metadata)
    support = context.candidate.metadata.get("signal_support")
    if not isinstance(support, Mapping):
        raise ValueError("candidate metadata has no signal support audit")
    _write_json(artifacts / "event_signal_support.json", support)

    study = _study_from_protocol(
        experiment_dir / "runtime",
        context.protocol,
        protocol_digest,
        storage_mode=storage_mode,
    )
    recovered = recover_running_trials(study) if recover_runtime else ()
    initial_params = trial_params_for_strategy(
        context.origin_weights,
        float(context.ex04["spec"]["enter"]),
        float(context.ex04["spec"]["exit"]),
        context.protocol,
    )
    enqueue_initial_trial(study, initial_params)
    evaluation_inputs = TrialEvaluationInputs.from_context(
        context, fee_rate=fee_rate, init_cash=init_cash
    )
    factor_names = tuple(context.candidate.factors.columns)

    def request_factory(trial: optuna.Trial) -> TrialRequest:
        strategy = suggest_trial_parameters(
            trial, factor_names, context.origin_weights, context.protocol
        )
        return TrialRequest(trial.number, strategy)

    config = context.protocol["optuna"]
    parallel = context.protocol["parallel"]
    started_at = datetime.now(timezone.utc).isoformat()
    no_improvement = config["no_improvement_trials"]
    wall_time = config["maximum_wall_time_seconds"]
    search = run_study_batches(
        study,
        request_factory,
        lambda requests: evaluate_trial_batch(
            requests, evaluation_inputs, n_jobs=int(parallel["n_jobs"])
        ),
        maximum_completed_trials=int(config["maximum_completed_trials"]),
        minimum_completed_trials=int(config["minimum_completed_trials"]),
        no_improvement_trials=(
            int(no_improvement) if no_improvement is not None else None
        ),
        maximum_wall_time_seconds=float(wall_time) if wall_time is not None else None,
        batch_size=int(config["batch_size"]),
        collect_batch_timings=collect_batch_timings,
    )
    if require_full_trial_count and int(search["completed_trials"]) != int(
        config["maximum_completed_trials"]
    ):
        raise AssertionError("formal search did not complete every preregistered Trial")
    batch_timings = search.pop("batch_timings", None)
    timing_summary: dict[str, object] | None = None
    if batch_timings is not None:
        timing_frame = pd.DataFrame(batch_timings)
        timing_frame.to_csv(
            artifacts / "batch_timings.csv", index=False, encoding="utf-8-sig"
        )
        timing_summary = {
            "batch_count": len(timing_frame),
            "ask_and_project_seconds": float(
                timing_frame["ask_and_project_seconds"].sum()
            ),
            "parallel_evaluate_seconds": float(
                timing_frame["parallel_evaluate_seconds"].sum()
            ),
            "tell_and_attrs_seconds": float(
                timing_frame["tell_and_attrs_seconds"].sum()
            ),
            "batch_total_seconds": float(timing_frame["batch_total_seconds"].sum()),
        }
    export_study_trials(study, artifacts / "trials.csv")
    ranked = _completed_trial_rows(study)
    ranked.to_csv(artifacts / "trial_ranking.csv", index=False, encoding="utf-8-sig")
    frozen, frozen_digest, verified = _freeze_winner(
        study, context, artifacts, evaluation_inputs
    )
    failed_count = sum(
        trial.state == optuna.trial.TrialState.FAIL for trial in study.trials
    )
    summary = {
        **search,
        "started_at_utc": started_at,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "failed_trials": int(failed_count),
        "recovered_running_trials": list(recovered),
        "best_trial_number": int(frozen["trial_number"]),
        "best_objective": float(verified.value),
        "candidate_factor_count": len(factor_names),
        "final_active_factor_count": len(frozen["factor_names"]),
        "visible_data_hashes": context.data.hashes,
        "holdout_accessed": False,
        "optuna_version": optuna.__version__,
        "storage_mode": storage_mode,
        "execution_commit": execution_commit,
        "timing_summary": timing_summary,
    }
    _write_json(artifacts / "study_summary.json", summary)
    holdout = _run_holdout(
        raw_dir,
        context,
        frozen,
        frozen_digest,
        artifacts,
        fee_rate=fee_rate,
        init_cash=init_cash,
    )
    summary["holdout_accessed"] = True
    summary["holdout_status"] = holdout["status"]
    summary["frozen_challenger_sha256"] = frozen_digest
    summary["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    _write_json(artifacts / "study_summary.json", summary)
    _write_research_docs(experiment_dir, summary, frozen, holdout)
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": str(context.protocol["experiment_id"]),
            "date": "2026-08-24",
            "status": holdout["status"],
            "symbol": "588080.SH",
            "asset_type": "etf",
            "research_baseline": context.protocol["research_baseline"],
            "candidate_source": context.protocol["candidate_source"],
            "visible_sample_end": "2025-12-31",
            "holdout_accessed": True,
            "frozen_challenger_sha256": frozen_digest,
        },
    )
    validate_experiment_archive(experiment_dir)
    return {
        "status": holdout["status"],
        "best_trial_number": frozen["trial_number"],
        "selection": summary,
        "holdout": holdout,
        "experiment_dir": str(experiment_dir),
    }


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument(
        "--baseline-root", type=Path, default=Path("configs/rule_baselines")
    )
    parser.add_argument(
        "--experiment-dir", type=Path, default=Path("experiments/0824_EX07")
    )
    parser.add_argument("--storage-mode", choices=("sqlite", "memory"), default="sqlite")
    parser.add_argument("--require-full-trial-count", action="store_true")
    parser.add_argument("--execution-commit")
    args = parser.parse_args()
    protocol = _read_json(args.experiment_dir / "artifacts" / "protocol.json")
    experiment_id = str(protocol.get("experiment_id", ""))
    protocol_validator = (
        validate_ex08_protocol if experiment_id == "0824_EX08" else validate_ex07_protocol
    )
    declared_storage = protocol.get("storage_mode")
    if declared_storage is not None and declared_storage != args.storage_mode:
        parser.error("storage mode differs from preregistered protocol")
    result = run_optuna_experiment(
        args.raw_dir,
        args.baseline_root,
        args.experiment_dir,
        protocol_validator=protocol_validator,
        storage_mode=args.storage_mode,
        recover_runtime=args.storage_mode == "sqlite",
        collect_batch_timings=args.storage_mode == "memory",
        require_full_trial_count=args.require_full_trial_count,
        execution_commit=args.execution_commit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    cli()
