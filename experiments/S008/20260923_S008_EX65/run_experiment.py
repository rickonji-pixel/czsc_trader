from __future__ import annotations

# Native math libraries must not multiply the eight frozen worker processes.
import os

for _thread_variable in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ[_thread_variable] = "1"

from concurrent.futures import ProcessPoolExecutor
import hashlib
import importlib.util
import json
import multiprocessing
from pathlib import Path
import platform
import shutil
import sys
import time
from typing import Any, Mapping

import numpy as np
import optuna
from optuna.trial import TrialState
import pandas as pd
from dotenv import load_dotenv
from strategy_runtime import StrategyCandidate, StrategyInit, StrategyRuntime, TradableWindow
from strategy_runtime.implementation_identity import implementation_sha256
from trading_execution_engine import HistoricalExecutor

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX65"
_PROTOTYPE_ID = "S008-P04-PRECIOUS-METAL-PREFERENCE"
_INITIAL_CASH = 1_000_000.0
_WORKER: dict[str, Any] = {}


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_module(path: Path, name: str):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def _candidate(
    source_root: Path,
    runtime_identity: str,
    candidate_id: str,
    parameters: Mapping[str, object],
) -> StrategyCandidate:
    return StrategyCandidate(
        "S008",
        candidate_id,
        {
            "runtime": {
                "module": "strategy_runtime.strategies.s008_precious_metal_preference",
                "qualname": "S008PreciousMetalPreference",
                "contract_version": 1,
                "source_files": ["strategies/s008_precious_metal_preference.py"],
                "source_sha256": runtime_identity,
            },
            "parameters": {"prototype_id": _PROTOTYPE_ID, **dict(parameters)},
        },
        source_root,
    )


def _anchor() -> dict[str, object]:
    return {
        "slow_entry": 0.01,
        "slow_exit": -0.01,
        "fast_entry": 0.01,
        "fast_exit": -0.01,
        "confirmation_sessions": 3,
        "minimum_hold_sessions": 10,
        "cooldown_sessions": 5,
    }


def _state_history(parameters: Mapping[str, object]) -> pd.DataFrame:
    bundle = _WORKER["bundle"]
    index = pd.DatetimeIndex(bundle["signal_sessions"])
    slow = pd.Series(bundle["slow"], index=index, dtype=float)
    fast = pd.Series(bundle["fast"], index=index, dtype=float)
    state = "FLAT"
    entry_streak = 0
    hold_sessions = 0
    cooldown_remaining = 0
    targets: list[float] = []
    states: list[str] = []
    actions: list[str] = []
    streaks: list[int] = []
    holds: list[int] = []
    cooldowns: list[int] = []
    for slow_value, fast_value in zip(slow, fast, strict=True):
        ready = pd.notna(slow_value) and pd.notna(fast_value)
        action = "HOLD_FLAT" if state == "FLAT" else "HOLD_LONG"
        if state == "FLAT":
            hold_sessions = 0
            if not ready:
                entry_streak = 0
                action = "WARMUP"
            elif cooldown_remaining > 0:
                cooldown_remaining -= 1
                entry_streak = 0
                action = "COOLDOWN"
            else:
                entry_streak = (
                    entry_streak + 1
                    if slow_value >= float(parameters["slow_entry"])
                    and fast_value >= float(parameters["fast_entry"])
                    else 0
                )
                if entry_streak >= int(parameters["confirmation_sessions"]):
                    state = "LONG"
                    hold_sessions = 1
                    entry_streak = 0
                    action = "ENTER"
        else:
            hold_sessions += 1
            if (
                ready
                and hold_sessions >= int(parameters["minimum_hold_sessions"])
                and (
                    slow_value <= float(parameters["slow_exit"])
                    or fast_value <= float(parameters["fast_exit"])
                )
            ):
                state = "FLAT"
                hold_sessions = 0
                cooldown_remaining = int(parameters["cooldown_sessions"])
                entry_streak = 0
                action = "EXIT"
        targets.append(1.0 if state == "LONG" else 0.0)
        states.append(state)
        actions.append(action)
        streaks.append(entry_streak)
        holds.append(hold_sessions)
        cooldowns.append(cooldown_remaining)
    return pd.DataFrame(
        {
            "gold_silver_ratio_distance_120": slow,
            "gold_minus_silver_return_20": fast,
            "xau_source_date": pd.to_datetime(bundle["xau_source_dates"]),
            "xag_source_date": pd.to_datetime(bundle["xag_source_dates"]),
            "target_position": targets,
            "state": states,
            "action": actions,
            "entry_streak": streaks,
            "hold_sessions": holds,
            "cooldown_remaining": cooldowns,
            "prototype_id": _PROTOTYPE_ID,
        },
        index=index,
    )


def _run_account(candidate: StrategyCandidate, history: pd.DataFrame, fee_rate: float):
    bundle = _WORKER["bundle"]
    utilities = _WORKER["utilities"]
    window = TradableWindow(
        pd.Timestamp(bundle["evaluation_start"]).date(),
        pd.Timestamp(bundle["evaluation_end"]).date(),
    )
    runtime = StrategyRuntime()
    definition = runtime.describe(candidate)
    policy = (
        definition.execution
        if fee_rate == 0.001
        else utilities._stress_policy(definition.execution, fee_rate)
    )
    instance = runtime.create(
        StrategyInit(
            candidate,
            window,
            Path(bundle["worker_data_dir"]),
            execution_policy=policy,
        )
    )
    prepared = utilities._SearchPreparedData(instance.identity, bundle)
    instance._prepared_data = prepared
    sessions = pd.DatetimeIndex(bundle["signal_sessions"], name="dt")
    instance._history_cache[(prepared.dataset_identity, "window")] = (
        history.copy(),
        sessions,
    )
    executor = HistoricalExecutor(
        strategy_reference=instance.definition.release_id,
        symbol=instance.identity.symbol,
        execution_daily=bundle["execution_daily"],
        execution_intraday=pd.DataFrame(columns=["dt", "high", "low"]),
        evaluation_start=pd.Timestamp(bundle["evaluation_start"]),
        evaluation_end=pd.Timestamp(bundle["evaluation_end"]),
        initial_cash=_INITIAL_CASH,
        execution_policy=instance.execution_policy,
        order_types=instance.definition.capabilities.order_types,
        account_id=f"{candidate.candidate_id}-{int(fee_rate * 10000)}bp",
    )
    return instance.run_window(executor=executor)


def _worker_init(bundle: Mapping[str, Any]) -> None:
    global _WORKER
    utility_path = Path(str(bundle["utility_path"]))
    utilities = _load_module(utility_path, f"s008_ex61_util_{os.getpid()}")
    _WORKER = {"bundle": dict(bundle), "utilities": utilities}


def _evaluate_worker(task: Mapping[str, Any]) -> dict[str, object]:
    parameters = dict(task["parameters"])
    history = _state_history(parameters)
    candidate = _candidate(
        Path(_WORKER["bundle"]["source_root"]),
        str(_WORKER["bundle"]["runtime_identity"]),
        f"EX65T{int(task['trial_number']):06d}",
        parameters,
    )
    primary = _run_account(candidate, history, 0.001)
    stress = _run_account(candidate, history, 0.003)
    utilities = _WORKER["utilities"]
    return {
        "primary": utilities._account_metrics(primary),
        "stress": utilities._account_metrics(stress),
        "behavior_sha256": hashlib.sha256(
            history["target_position"].to_numpy(dtype="int8").tobytes()
        ).hexdigest(),
    }


def _suggest(trial: optuna.Trial, space: Mapping[str, object]) -> dict[str, object]:
    return {
        name: trial.suggest_categorical(name, list(values))
        for name, values in space.items()
    }


def _constraints(parameters: Mapping[str, object]) -> list[str]:
    failures = []
    if float(parameters["slow_exit"]) >= float(parameters["slow_entry"]):
        failures.append("SLOW_EXIT_NOT_BELOW_ENTRY")
    if float(parameters["fast_exit"]) >= float(parameters["fast_entry"]):
        failures.append("FAST_EXIT_NOT_BELOW_ENTRY")
    return failures


def _trial_row(
    *, protocol_hash: str, seed: int, trial_number: int,
    parameters: Mapping[str, object], failures: list[str], trial_state: str,
    result: Mapping[str, object] | None, baseline: Mapping[str, object],
    data_identity: Mapping[str, object], runtime_identity: str,
    exception: BaseException | None = None,
) -> dict[str, object]:
    primary = dict(result["primary"]) if result is not None else {}
    stress = dict(result["stress"]) if result is not None else {}
    baseline_primary = dict(baseline["primary"])
    annualized = primary.get("annualized_return")
    drawdown = primary.get("maximum_drawdown_magnitude")
    annual_pass = bool(
        annualized is not None
        and float(annualized) >= float(baseline_primary["annualized_return"]) * 1.5
    )
    drawdown_pass = bool(
        drawdown is not None
        and float(drawdown) < float(baseline_primary["maximum_drawdown_magnitude"])
    )
    return {
        "experiment_id": EXPERIMENT_ID,
        "prototype_id": _PROTOTYPE_ID,
        "study_seed": seed,
        "trial_number": trial_number,
        "parameters_json": _json(parameters),
        "constraint_status": "PASS" if not failures else "INVALID",
        "constraint_failures_json": _json(failures),
        "trial_state": trial_state,
        "exception_type": "" if exception is None else type(exception).__name__,
        "exception_message": "" if exception is None else str(exception),
        "evaluation_start": str(data_identity["evaluation_start"]),
        "evaluation_end": str(data_identity["evaluation_end"]),
        "primary_strategy_metrics_json": _json(primary),
        "stress_strategy_metrics_json": _json(stress),
        "primary_buyhold_metrics_json": _json(baseline["primary"]),
        "stress_buyhold_metrics_json": _json(baseline["stress"]),
        "annualized_return_objective": annualized,
        "maximum_drawdown_magnitude_objective": drawdown,
        "annualized_return_gate_pass": annual_pass,
        "maximum_drawdown_gate_pass": drawdown_pass,
        "hard_qualified": bool(annual_pass and drawdown_pass),
        "entry_count": primary.get("entry_count"),
        "closed_trade_count": primary.get("closed_trade_count"),
        "exposure_ratio": primary.get("exposure_ratio"),
        "behavior_sha256": "" if result is None else result["behavior_sha256"],
        "data_identity_json": _json(data_identity),
        "protocol_sha256": protocol_hash,
        "runtime_implementation_sha256": runtime_identity,
        "python_version": platform.python_version(),
        "optuna_version": optuna.__version__,
    }


def _checkpoint(path: Path, rows: list[dict[str, object]]) -> None:
    frame = pd.DataFrame(rows).sort_values("trial_number")
    frame.to_csv(
        path,
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )


def _pareto(frame: pd.DataFrame) -> pd.Series:
    values = frame[
        ["annualized_return_objective", "maximum_drawdown_magnitude_objective"]
    ].to_numpy(dtype=float)
    keep = pd.Series(True, index=frame.index)
    for offset, index in enumerate(frame.index):
        current = values[offset]
        dominates = (
            (values[:, 0] >= current[0])
            & (values[:, 1] <= current[1])
            & ((values[:, 0] > current[0]) | (values[:, 1] < current[1]))
        )
        dominates[offset] = False
        if dominates.any():
            keep.loc[index] = False
    return keep


def _safe_reset(path: Path, parent: Path) -> None:
    target = path.resolve()
    root = parent.resolve()
    if target.parent != root or not target.name.startswith("S008_EX65_"):
        raise ValueError("unsafe EX65 temporary path")
    if target.exists():
        shutil.rmtree(target)


def main() -> None:
    started = time.perf_counter()
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if not protocol.get("reads_real_returns") or not protocol.get("starts_search"):
        raise ValueError("formal search must explicitly declare development return use")
    if any(
        protocol.get(key)
        for key in (
            "selects_winner", "candidate_generation", "reads_sealed_validation",
            "mutates_catalog", "mutates_platform", "mutates_pte",
        )
    ):
        raise ValueError("formal search scope exceeds RSCH authority")

    runtime_root = repo / "experiments/S008/20260923_S008_EX64/runtime/strategy_runtime"
    runtime_path = runtime_root / "strategies/s008_precious_metal_preference.py"
    utility_path = repo / "experiments/S008/20260923_S008_EX38/run_experiment.py"
    frozen_paths = {
        "ex60_manifest_sha256": repo / "experiments/S008/20260923_S008_EX60/experiment_manifest.json",
        "ex64_manifest_sha256": repo / "experiments/S008/20260923_S008_EX64/experiment_manifest.json",
        "runtime_file_sha256": runtime_path,
        "materials_sha256": repo / "research/S008/materials.json",
        "execution_manifest_sha256": repo / "data/raw/518880_execution_manifest.json",
        "srt_strategy_sha256": repo / "packages/strategy_runtime/src/strategy_runtime/strategy.py",
        "srt_planner_sha256": repo / "packages/strategy_runtime/src/strategy_runtime/execution_planner.py",
        "txe_historical_sha256": repo / "packages/trading_execution_engine/src/trading_execution_engine/historical.py",
        "search_utility_sha256": utility_path,
    }
    for key, path in frozen_paths.items():
        if _sha256(path) != protocol["sources"][key]:
            raise ValueError(f"frozen search source differs: {path}")
    validate_experiment_archive(repo / "experiments/S008/20260923_S008_EX60")
    runtime_identity = implementation_sha256(
        tuple(protocol["runtime"]["source_files"]), source_root=runtime_root
    )
    if runtime_identity != protocol["sources"]["runtime_source_sha256"]:
        raise ValueError("runtime source differs from frozen search contract")

    search = dict(protocol["search_execution"])
    if (
        search["storage"] != "optuna.storages.InMemoryStorage"
        or int(search["logical_workers"]) != 8
        or int(search["trial_budget"]) != 1500
        or bool(search["resume"])
    ):
        raise ValueError("search execution differs from EX60")
    load_dotenv(repo / str(protocol["dotenv_path"]), override=False)
    utilities = _load_module(utility_path, "s008_ex61_parent_util")

    evaluation_start = pd.Timestamp(protocol["evaluation_start"])
    evaluation_end = pd.Timestamp(protocol["development_cutoff"])
    tmp_parent = repo / ".tmp"
    tmp_parent.mkdir(exist_ok=True)
    prepared_root = tmp_parent / "S008_EX65_prepared"
    _safe_reset(prepared_root, tmp_parent)
    anchor_candidate = _candidate(runtime_root, runtime_identity, "EX65ANCHOR", _anchor())
    instance = StrategyRuntime().create(
        StrategyInit(
            anchor_candidate,
            TradableWindow(evaluation_start.date(), evaluation_end.date()),
            prepared_root,
        )
    )
    prepared_summary = instance.prepare_data()
    full_anchor_history = instance.inspect_signals()
    base_data = instance._prepared_data
    if base_data is None:
        raise ValueError("SRT did not retain prepared data")
    signal_dates = {
        trading: base_data.signal_date_for(trading)
        for trading in base_data.trading_dates()
    }
    signal_sessions = tuple(pd.Timestamp(value) for value in signal_dates.values())
    if len(signal_sessions) != len(set(signal_sessions)):
        raise ValueError("signal sessions are not unique")
    full_anchor_history = full_anchor_history.reindex(pd.DatetimeIndex(signal_sessions))
    if full_anchor_history[[
        "gold_silver_ratio_distance_120", "gold_minus_silver_return_20"
    ]].isna().any().any():
        raise ValueError("formal evaluation contains an unready precious-metal feature")
    strict_prior = bool(
        (pd.to_datetime(full_anchor_history["xau_source_date"]) < full_anchor_history.index).all()
        and (pd.to_datetime(full_anchor_history["xag_source_date"]) < full_anchor_history.index).all()
    )
    if not strict_prior:
        raise ValueError("formal FXCM inputs violate strict prior-date causality")
    signal_index = full_anchor_history.index.to_series(index=full_anchor_history.index)
    xau_age = (signal_index - pd.to_datetime(full_anchor_history["xau_source_date"])).dt.days
    xag_age = (signal_index - pd.to_datetime(full_anchor_history["xag_source_date"])).dt.days
    if (xau_age > 7).any() or (xag_age > 7).any():
        raise ValueError("formal FXCM inputs exceed frozen seven-day staleness")

    execution_daily = utilities._load_execution_daily(
        repo,
        repo / "data/raw/518880_execution_manifest.json",
        evaluation_end,
    )
    adjusted = base_data.adjusted_daily
    bundle = {
        "utility_path": str(utility_path),
        "source_root": str(runtime_root),
        "runtime_identity": runtime_identity,
        "slow": full_anchor_history["gold_silver_ratio_distance_120"].to_numpy(dtype=float),
        "fast": full_anchor_history["gold_minus_silver_return_20"].to_numpy(dtype=float),
        "xau_source_dates": tuple(full_anchor_history["xau_source_date"]),
        "xag_source_dates": tuple(full_anchor_history["xag_source_date"]),
        "adjusted_daily": adjusted,
        "execution_daily": execution_daily,
        "input_identities": dict(base_data.input_identities),
        "price_identities": dict(base_data.price_identities),
        "base_data_identity": prepared_summary.data_identity,
        "evaluation_start": evaluation_start,
        "evaluation_end": evaluation_end,
        "signal_dates": signal_dates,
        "signal_sessions": signal_sessions,
        "worker_data_dir": str(tmp_parent / "S008_EX65_worker"),
    }
    _worker_init(bundle)
    accelerated_anchor_history = _state_history(_anchor())
    pd.testing.assert_frame_equal(
        accelerated_anchor_history.loc[:, full_anchor_history.columns],
        full_anchor_history,
        check_dtype=False,
        check_exact=False,
        atol=1e-12,
        rtol=1e-12,
    )
    full_executor = HistoricalExecutor(
        strategy_reference=instance.definition.release_id,
        symbol=instance.identity.symbol,
        execution_daily=execution_daily,
        execution_intraday=pd.DataFrame(columns=["dt", "high", "low"]),
        evaluation_start=evaluation_start,
        evaluation_end=evaluation_end,
        initial_cash=_INITIAL_CASH,
        execution_policy=instance.execution_policy,
        order_types=instance.definition.capabilities.order_types,
        account_id="EX65-anchor-full",
    )
    full_anchor_account = instance.run_window(executor=full_executor)
    accelerated_anchor_account = _run_account(anchor_candidate, accelerated_anchor_history, 0.001)
    full_equity = full_anchor_account.account_daily["equity"].to_numpy(dtype=float)
    accelerated_equity = accelerated_anchor_account.account_daily["equity"].to_numpy(dtype=float)
    if not np.allclose(full_equity, accelerated_equity, rtol=0.0, atol=1e-8):
        raise ValueError("accelerated account differs from complete SRT/TXE anchor")

    buyhold_history = accelerated_anchor_history.copy()
    buyhold_history["target_position"] = 1.0
    buyhold_candidate = _candidate(runtime_root, runtime_identity, "EX65BUYHOLD", _anchor())
    baseline = {
        "primary": utilities._account_metrics(
            _run_account(buyhold_candidate, buyhold_history, 0.001)
        ),
        "stress": utilities._account_metrics(
            _run_account(buyhold_candidate, buyhold_history, 0.003)
        ),
    }
    data_identity = {
        "prepared_data_identity": prepared_summary.data_identity,
        "input_identities": dict(base_data.input_identities),
        "price_identities": dict(base_data.price_identities),
        "execution_manifest_sha256": protocol["sources"]["execution_manifest_sha256"],
        "evaluation_start": evaluation_start.date().isoformat(),
        "evaluation_end": evaluation_end.date().isoformat(),
        "signal_session_count": len(signal_sessions),
    }
    _write(
        artifacts / "input_and_acceleration_evidence.json",
        {
            "schema_version": 1,
            "experiment_id": EXPERIMENT_ID,
            "data_identity": data_identity,
            "strict_prior_fxcm_source_dates": True,
            "anchor_signal_history_matches_complete_srt": True,
            "anchor_account_equity_matches_complete_srt_txe": True,
            "runtime_implementation_sha256": runtime_identity,
            "logical_workers": 8,
            "native_threads_per_worker": 1,
        },
    )
    _write(artifacts / "buyhold_metrics.json", baseline)

    seed = int(search["seed"])
    storage = optuna.storages.InMemoryStorage()
    sampler = optuna.samplers.NSGAIISampler(
        seed=seed,
        population_size=int(search["sampler_population_size"]),
        constraints_func=lambda frozen: frozen.user_attrs.get("constraints", [1.0, 1.0]),
    )
    study = optuna.create_study(
        study_name=f"{EXPERIMENT_ID}-{_PROTOTYPE_ID}",
        directions=["maximize", "minimize"],
        storage=storage,
        sampler=sampler,
        pruner=optuna.pruners.NopPruner(),
    )
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    rows: list[dict[str, object]] = []
    ledger_path = artifacts / "search_trial_ledger.csv.gz"
    budget = int(search["trial_budget"])
    batch_size = int(search["evaluation_batch_size"])
    protocol_hash = _sha256(protocol_path)
    context = multiprocessing.get_context("spawn")
    search_started = time.perf_counter()
    with ProcessPoolExecutor(
        max_workers=int(search["logical_workers"]),
        mp_context=context,
        initializer=_worker_init,
        initargs=(bundle,),
    ) as pool:
        for batch_start in range(0, budget, batch_size):
            batch = []
            futures = {}
            for _ in range(min(batch_size, budget - batch_start)):
                trial = study.ask()
                parameters = _suggest(trial, protocol["search_space"])
                failures = _constraints(parameters)
                trial.set_user_attr(
                    "constraints", [1.0 if "SLOW_EXIT_NOT_BELOW_ENTRY" in failures else 0.0,
                                    1.0 if "FAST_EXIT_NOT_BELOW_ENTRY" in failures else 0.0]
                )
                batch.append((trial, parameters, failures))
                if not failures:
                    futures[trial.number] = pool.submit(
                        _evaluate_worker,
                        {"trial_number": trial.number, "parameters": parameters},
                    )
            for trial, parameters, failures in sorted(batch, key=lambda item: item[0].number):
                if failures:
                    study.tell(
                        trial,
                        values=(
                            float(search["invalid_annualized_return"]),
                            float(search["invalid_maximum_drawdown_magnitude"]),
                        ),
                    )
                    row = _trial_row(
                        protocol_hash=protocol_hash, seed=seed, trial_number=trial.number,
                        parameters=parameters, failures=failures,
                        trial_state="CONSTRAINT_INVALID", result=None,
                        baseline=baseline, data_identity=data_identity,
                        runtime_identity=runtime_identity,
                    )
                else:
                    try:
                        result = futures[trial.number].result()
                        annualized = float(result["primary"]["annualized_return"])
                        drawdown = float(result["primary"]["maximum_drawdown_magnitude"])
                        if not np.isfinite(annualized) or not np.isfinite(drawdown):
                            raise ValueError("trial metrics are not finite")
                        study.tell(trial, values=(annualized, drawdown))
                        row = _trial_row(
                            protocol_hash=protocol_hash, seed=seed, trial_number=trial.number,
                            parameters=parameters, failures=[], trial_state="COMPLETE",
                            result=result, baseline=baseline, data_identity=data_identity,
                            runtime_identity=runtime_identity,
                        )
                    except BaseException as exc:
                        study.tell(trial, state=TrialState.FAIL)
                        row = _trial_row(
                            protocol_hash=protocol_hash, seed=seed, trial_number=trial.number,
                            parameters=parameters, failures=[], trial_state="EXECUTION_FAILED",
                            result=None, baseline=baseline, data_identity=data_identity,
                            runtime_identity=runtime_identity, exception=exc,
                        )
                        rows.append(row)
                        _checkpoint(ledger_path, rows)
                        raise RuntimeError(f"EX65 trial {trial.number} failed") from exc
                rows.append(row)
            _checkpoint(ledger_path, rows)
            completed = min(batch_start + batch_size, budget)
            qualified_count = sum(bool(row["hard_qualified"]) for row in rows)
            print(f"{_PROTOTYPE_ID}: {completed}/{budget}; qualified={qualified_count}", flush=True)

    ledger = pd.DataFrame(rows).sort_values("trial_number")
    if len(ledger) != budget or ledger["trial_state"].eq("EXECUTION_FAILED").any():
        raise ValueError("formal search ledger is incomplete")
    qualified = ledger.loc[ledger["hard_qualified"].eq(True)].copy()
    pareto = qualified.loc[_pareto(qualified)].copy() if len(qualified) else qualified.copy()
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    qualified.to_csv(
        artifacts / "hard_qualified_trials.csv.gz", index=False,
        compression=compression, lineterminator="\n",
    )
    pareto.to_csv(
        artifacts / "hard_qualified_pareto.csv.gz", index=False,
        compression=compression, lineterminator="\n",
    )
    decision = (
        "PROCEED_TO_PRECIOUS_METAL_PARAMETER_PLATFORM_AUDIT"
        if len(qualified)
        else "STOP_PRECIOUS_METAL_PREFERENCE_NO_FEASIBLE_REGION"
    )
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": decision,
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "search_runtime_seconds": round(time.perf_counter() - search_started, 3),
        "evaluation_start": evaluation_start.date().isoformat(),
        "evaluation_end": evaluation_end.date().isoformat(),
        "logical_workers": int(search["logical_workers"]),
        "storage": type(storage).__name__,
        "seed": seed,
        "total_attempted_trials": int(len(ledger)),
        "total_complete_trials": int(ledger["trial_state"].eq("COMPLETE").sum()),
        "total_constraint_invalid_trials": int(
            ledger["trial_state"].eq("CONSTRAINT_INVALID").sum()
        ),
        "total_hard_qualified_trials": int(len(qualified)),
        "hard_qualified_pareto_trials": int(len(pareto)),
        "unique_hard_qualified_behaviors": int(qualified["behavior_sha256"].nunique())
        if len(qualified) else 0,
        "buyhold_primary": baseline["primary"],
        "buyhold_stress": baseline["stress"],
        "winner_selected": False,
        "candidate_created": False,
        "sealed_validation_read": False,
        "catalog_mutated": False,
        "platform_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "search_evidence.json", evidence)
    _safe_reset(prepared_root, tmp_parent)
    (experiment / "03_execution.md").write_text(
        "# S008 EX65 执行记录\n\n"
        f"完成贵金属偏好原型的InMemoryStorage study，共{len(ledger)}次trial；8个工作进程并行"
        f"评价，完整有效{evidence['total_complete_trials']}次，约束无效"
        f"{evidence['total_constraint_invalid_trials']}次。DFLS因果、完整SRT信号和完整TXE账户等价门"
        "均在搜索前通过；未读取封存期、选择参数或创建候选。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX65 结论\n\n"
        f"机器裁决：`{decision}`。主成本下共有{len(qualified)}个trial同时满足年化收益与最大回撤"
        f"两项硬门，覆盖{evidence['unique_hard_qualified_behaviors']}种独立目标仓位行为，其中非支配"
        f"合格点{len(pareto)}个。\n\n"
        "本实验只判断冻结搜索空间是否存在可行区域，不选择冠军参数。存在合格点时，下一步必须"
        "审查连通平台、局部可行率和代表点稳定性。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "credential_id": protocol["credential_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": decision,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()

