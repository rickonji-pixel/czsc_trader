from __future__ import annotations

# ruff: noqa: E402 -- native thread limits must be set before NumPy is imported.

import os

for _thread_variable in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ[_thread_variable] = "1"

from concurrent.futures import ProcessPoolExecutor
from datetime import date
from decimal import Decimal
import hashlib
import importlib.util
import json
import multiprocessing
from pathlib import Path
import platform
import shutil
import sys
import time
from types import SimpleNamespace
from typing import Any, Mapping

import numpy as np
import optuna
from optuna.trial import TrialState
import pandas as pd
from dotenv import load_dotenv
from strategy_runtime import (
    ExecutionPolicy,
    PriceReference,
    StrategyCandidate,
    StrategyInit,
    StrategyRuntime,
    TradableWindow,
)
from strategy_runtime.implementation_identity import implementation_sha256
from trading_execution_engine import HistoricalExecutor

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX36"
_INITIAL_CASH = 1_000_000.0
_COMPONENTS = (
    "price_return_5d",
    "price_return_120d",
    "price_trend_distance_20",
    "currency_usdcnh_return_20d",
    "rate_shibor_overnight_level",
    "risk_sse_drawdown_60",
    "risk_sse_return_5d",
    "risk_sse_return_20d",
    "risk_sse_turnover_z20",
    "macro_domestic_real_cash_proxy",
    "price_volatility_ratio_20_60",
    "tsfresh_log_volume_change_absolute_sum_of_changes_60",
    "tsfresh_price_intraday_range_mean_abs_change_60",
)
_INTEGER_PARAMETERS = {
    "maximum_wait_sessions",
    "maximum_hold_sessions",
    "shock_quorum",
}
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


def _content_hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _load_runtime(path: Path, name: str):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load runtime: {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def _anchor_rule() -> dict[str, object]:
    return {
        "context_weights": {
            "liquidity_context": 1.0,
            "equity_stress_context": 1.0,
            "market_state_context": 1.0,
        },
        "timing_weights": {
            "price_return_5d": 1.0,
            "price_return_120d": 1.0,
            "price_trend_distance_20": 1.0,
        },
        "opportunity_entry": 0.5,
        "context_entry": 0.5,
        "timing_entry": 0.5,
        "opportunity_exit": 0.35,
        "context_exit": 0.35,
    }


def _candidate(
    source_root: Path,
    runtime_identity: str,
    prototype_id: str,
    candidate_id: str,
    rule: Mapping[str, object],
) -> StrategyCandidate:
    return StrategyCandidate(
        "S008",
        candidate_id,
        {
            "runtime": {
                "module": "strategy_runtime.strategies.s008_prototypes",
                "qualname": "S008Prototype",
                "contract_version": 1,
                "source_files": ["strategies/s008_prototypes.py"],
                "source_sha256": runtime_identity,
            },
            "parameters": {
                "prototype_id": prototype_id,
                "normalization": {
                    "lookback_sessions": 252,
                    "minimum_observations": 126,
                },
                "rule": dict(rule),
            },
        },
        source_root,
    )


def _load_execution_daily(
    repo: Path, manifest_path: Path, cutoff: pd.Timestamp
) -> pd.DataFrame:
    manifest = _read(manifest_path)
    frames: list[pd.DataFrame] = []
    for filename, record in sorted(manifest["files"].items()):
        if record["frequency"] != "daily" or int(record["year"]) > cutoff.year:
            continue
        path = repo / "data/raw" / filename
        if _sha256(path) != record["sha256"]:
            raise ValueError(f"execution file differs from manifest: {filename}")
        frames.append(pd.read_csv(path))
    if not frames:
        raise ValueError("execution manifest contains no development daily files")
    frame = pd.concat(frames, ignore_index=True)
    frame["dt"] = pd.to_datetime(frame.pop("date"), errors="raise").dt.normalize()
    frame = frame.loc[frame["dt"].le(cutoff)].sort_values("dt").reset_index(drop=True)
    if len(frame) != 2781 or frame["dt"].duplicated().any():
        raise ValueError("execution development sessions differ from the S008 data gate")
    frame = frame.rename(columns={"volume": "vol"})
    frame["symbol"] = "518880.SH"
    return frame


def _stress_policy(policy: ExecutionPolicy, fee_rate: float) -> ExecutionPolicy:
    settings = dict(policy.settings)
    settings["capital"] = {
        **dict(settings["capital"]),
        "fee_rate": float(fee_rate),
    }
    return ExecutionPolicy(policy.policy_type, settings)


class _SearchPreparedData:
    def __init__(self, identity, bundle: Mapping[str, Any]) -> None:
        self.strategy = identity
        self.tradable_window = TradableWindow(
            pd.Timestamp(bundle["evaluation_start"]).date(),
            pd.Timestamp(bundle["evaluation_end"]).date(),
        )
        self.available_through = pd.Timestamp(bundle["signal_sessions"][-1]).date()
        self.dataset_identity = hashlib.sha256(
            (str(bundle["base_data_identity"]) + identity.runtime_sha256).encode("utf-8")
        ).hexdigest()
        self.input_identities = dict(bundle["input_identities"])
        self.price_identities = dict(bundle["price_identities"])
        self._signal_dates = dict(bundle["signal_dates"])
        self._trading_dates = tuple(sorted(self._signal_dates))
        self._inputs = SimpleNamespace(results={}, signal_dates=self._signal_dates)
        adjusted = bundle["adjusted_daily"].copy()
        execution = bundle["execution_daily"].copy()
        adjusted["dt"] = pd.to_datetime(adjusted["dt"]).dt.normalize()
        execution["dt"] = pd.to_datetime(execution["dt"]).dt.normalize()
        self._adjusted = adjusted.set_index("dt")
        self._execution = execution.set_index("dt")

    def trading_dates(self) -> tuple[date, ...]:
        return self._trading_dates

    def signal_date_for(self, trading_date: date) -> date:
        return self._signal_dates[trading_date]

    def price_reference(self, signal_date: date, trading_date: date) -> PriceReference:
        if trading_date <= signal_date:
            raise ValueError("execution session must follow signal session")
        stamp = pd.Timestamp(signal_date)
        return PriceReference(
            Decimal(str(self._adjusted.loc[stamp, "close"])),
            Decimal(str(self._execution.loc[stamp, "close"])),
            "ADJUSTED_CLOSE",
            "UNADJUSTED_CLOSE",
        )


def _history_for_rule(
    prototype_id: str, rule: Mapping[str, object]
) -> pd.DataFrame:
    runtime = _WORKER["runtime"]
    normalized = _WORKER["normalized"]
    scores = runtime._common_scores(normalized, rule)
    if prototype_id == "S008-P01-COMPOSITE-GATE":
        history = runtime._composite_gate(scores, rule)
    elif prototype_id == "S008-P02-REGIME-RECOVERY":
        history = runtime._regime_recovery(scores, rule)
    elif prototype_id == "S008-P03-SAFE-HAVEN-PULSE":
        history = runtime._safe_haven_pulse(normalized, scores, rule)
    else:
        raise ValueError(f"unsupported prototype: {prototype_id}")
    history["prototype_id"] = prototype_id
    history = history.reindex(pd.DatetimeIndex(_WORKER["signal_sessions"]))
    target = pd.to_numeric(history["target_position"], errors="raise")
    if target.isna().any() or not target.isin([0, 1]).all():
        raise ValueError("accelerated runtime emitted invalid target positions")
    return history


def _run_account(
    candidate: StrategyCandidate,
    history: pd.DataFrame,
    fee_rate: float,
) -> object:
    bundle = _WORKER["bundle"]
    window = TradableWindow(
        pd.Timestamp(bundle["evaluation_start"]).date(),
        pd.Timestamp(bundle["evaluation_end"]).date(),
    )
    runtime = StrategyRuntime()
    definition = runtime.describe(candidate)
    policy = (
        definition.execution
        if fee_rate == 0.001
        else _stress_policy(definition.execution, fee_rate)
    )
    instance = runtime.create(
        StrategyInit(
            candidate,
            window,
            Path(bundle["worker_data_dir"]),
            execution_policy=policy,
        )
    )
    prepared = _SearchPreparedData(instance.identity, bundle)
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


def _period_return(equity: pd.Series, prior_equity: float) -> float:
    if equity.empty:
        return float("nan")
    return float(equity.iloc[-1] / prior_equity - 1.0)


def _account_metrics(result: object) -> dict[str, object]:
    account = result.account_daily.copy()
    account["date"] = pd.to_datetime(account["date"]).dt.normalize()
    equity = account.set_index("date")["equity"].astype(float)
    annualized = float((equity.iloc[-1] / _INITIAL_CASH) ** (252 / len(equity)) - 1)
    drawdown = equity.div(equity.cummax().clip(lower=_INITIAL_CASH)).sub(1.0)
    maximum_drawdown = float(abs(drawdown.min()))
    calmar = annualized / maximum_drawdown if maximum_drawdown > 1e-12 else None
    closed = result.trades.loc[result.trades["status"].eq("CLOSED")].copy()
    returns = pd.to_numeric(closed["net_return"], errors="coerce")
    gains = float(returns.loc[returns.gt(0)].sum())
    losses = float(abs(returns.loc[returns.lt(0)].sum()))
    if closed.empty:
        profit_factor, profit_factor_status = None, "NO_CLOSED_TRADES"
    elif losses <= 0:
        profit_factor, profit_factor_status = None, "NO_LOSSES"
    else:
        profit_factor, profit_factor_status = gains / losses, "VALID"
    entries = result.orders.loc[
        result.orders["side"].eq("BUY") & result.orders["status"].eq("FILLED")
    ]
    daily_returns = equity.pct_change()
    daily_returns.iloc[0] = equity.iloc[0] / _INITIAL_CASH - 1.0
    annual_returns = {
        str(year): float((1.0 + values).prod() - 1.0)
        for year, values in daily_returns.groupby(daily_returns.index.year)
    }
    thirds: dict[str, float] = {}
    for ordinal, positions in enumerate(np.array_split(np.arange(len(equity)), 3), start=1):
        segment = equity.iloc[positions]
        prior = _INITIAL_CASH if positions[0] == 0 else float(equity.iloc[positions[0] - 1])
        thirds[f"T{ordinal}"] = _period_return(segment, prior)
    return {
        "annualized_return": annualized,
        "maximum_drawdown_magnitude": maximum_drawdown,
        "calmar_ratio": calmar,
        "profit_factor": profit_factor,
        "profit_factor_status": profit_factor_status,
        "total_return": float(equity.iloc[-1] / _INITIAL_CASH - 1.0),
        "entry_count": int(len(entries)),
        "closed_trade_count": int(len(closed)),
        "exposure_ratio": float(account["quantity"].gt(0).mean()),
        "calendar_year_returns": annual_returns,
        "chronological_thirds": thirds,
    }


def _worker_init(bundle: Mapping[str, Any]) -> None:
    global _WORKER
    runtime_path = Path(str(bundle["runtime_path"]))
    runtime = _load_runtime(
        runtime_path,
        f"s008_ex36_worker_{os.getpid()}",
    )
    _WORKER = {
        "bundle": dict(bundle),
        "runtime": runtime,
        "normalized": bundle["normalized"],
        "signal_sessions": tuple(bundle["signal_sessions"]),
    }


def _evaluate_worker(task: Mapping[str, Any]) -> dict[str, object]:
    prototype_id = str(task["prototype_id"])
    trial_number = int(task["trial_number"])
    rule = dict(task["rule"])
    history = _history_for_rule(prototype_id, rule)
    candidate = _candidate(
        Path(_WORKER["bundle"]["source_root"]),
        str(_WORKER["bundle"]["runtime_identity"]),
        prototype_id,
        f"EX36P{int(task['prototype_ordinal']):02d}T{trial_number:06d}",
        rule,
    )
    primary = _run_account(candidate, history, 0.001)
    stress = _run_account(candidate, history, 0.003)
    primary_metrics = _account_metrics(primary)
    stress_metrics = _account_metrics(stress)
    return {
        "primary": primary_metrics,
        "stress": stress_metrics,
        "behavior_sha256": hashlib.sha256(
            history["target_position"].to_numpy(dtype="int8").tobytes()
        ).hexdigest(),
    }


def _normalize_weights(
    raw: Mapping[str, object], names: tuple[str, ...]
) -> tuple[dict[str, float], bool]:
    values = {name: float(raw[f"{name}_weight"]) for name in names}
    total = sum(values.values())
    if not np.isfinite(total) or total <= 0:
        return values, False
    return {name: value / total for name, value in values.items()}, True


def _effective_rule(
    prototype_id: str, raw: Mapping[str, object]
) -> tuple[dict[str, object], list[str], list[float]]:
    failures: list[str] = []
    weight_failure = 0.0
    threshold_failure = 0.0
    structural_failure = 0.0
    if prototype_id == "S008-P01-COMPOSITE-GATE":
        context, context_valid = _normalize_weights(
            raw,
            (
                "liquidity_context",
                "equity_stress_context",
                "market_state_context",
            ),
        )
        timing, timing_valid = _normalize_weights(
            raw,
            ("price_return_5d", "price_return_120d", "price_trend_distance_20"),
        )
        if not context_valid or not timing_valid:
            failures.append("ZERO_SUM_WEIGHT_FAMILY")
            weight_failure = 1.0
        if float(raw["opportunity_exit"]) >= float(raw["opportunity_entry"]):
            failures.append("OPPORTUNITY_EXIT_NOT_BELOW_ENTRY")
            threshold_failure = 1.0
        if float(raw["context_exit"]) >= float(raw["context_entry"]):
            failures.append("CONTEXT_EXIT_NOT_BELOW_ENTRY")
            threshold_failure = 1.0
        rule = {
            "context_weights": context,
            "timing_weights": timing,
            **{
                name: float(raw[name])
                for name in (
                    "opportunity_entry",
                    "context_entry",
                    "timing_entry",
                    "opportunity_exit",
                    "context_exit",
                )
            },
        }
    elif prototype_id == "S008-P02-REGIME-RECOVERY":
        context, context_valid = _normalize_weights(
            raw,
            (
                "liquidity_context",
                "equity_stress_context",
                "market_state_context",
            ),
        )
        if not context_valid:
            failures.append("ZERO_SUM_WEIGHT_FAMILY")
            weight_failure = 1.0
        if float(raw["regime_exit"]) >= float(raw["regime_entry"]):
            failures.append("REGIME_EXIT_NOT_BELOW_ENTRY")
            threshold_failure = 1.0
        rule = {
            "context_weights": context,
            "regime_entry": float(raw["regime_entry"]),
            "regime_exit": float(raw["regime_exit"]),
            "oversold_arm": float(raw["oversold_arm"]),
            "recovery_delta": float(raw["recovery_delta"]),
            "maximum_wait_sessions": int(raw["maximum_wait_sessions"]),
            "maximum_hold_sessions": int(raw["maximum_hold_sessions"]),
        }
    elif prototype_id == "S008-P03-SAFE-HAVEN-PULSE":
        if float(raw["shock_exit_quantile"]) >= float(raw["shock_quantile"]):
            failures.append("SHOCK_EXIT_NOT_BELOW_ENTRY")
            threshold_failure = 1.0
        rule = {
            "context_weights": {
                "liquidity_context": 1.0,
                "equity_stress_context": 1.0,
                "market_state_context": 1.0,
            },
            "shock_quantile": float(raw["shock_quantile"]),
            "shock_quorum": int(raw["shock_quorum"]),
            "timing_minimum": float(raw["timing_minimum"]),
            "state_stability_minimum": float(raw["state_stability_minimum"]),
            "shock_exit_quantile": float(raw["shock_exit_quantile"]),
            "maximum_hold_sessions": int(raw["maximum_hold_sessions"]),
        }
    else:
        failures.append("UNKNOWN_PROTOTYPE")
        structural_failure = 1.0
        rule = {}
    return rule, failures, [weight_failure, threshold_failure, structural_failure]


def _suggest(trial: optuna.Trial, search_space: Mapping[str, object]) -> dict[str, object]:
    raw: dict[str, object] = {}
    for name, bounds in search_space.items():
        low, high = bounds
        if name in _INTEGER_PARAMETERS:
            raw[name] = trial.suggest_int(name, int(low), int(high))
        else:
            raw[name] = trial.suggest_float(name, float(low), float(high))
    return raw


def _checkpoint(path: Path, rows: list[dict[str, object]]) -> None:
    frame = pd.DataFrame(rows).sort_values(["prototype_id", "trial_number"])
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    frame.to_csv(path, index=False, compression=compression, lineterminator="\n")


def _trial_row(
    *,
    protocol_hash: str,
    prototype_id: str,
    seed: int,
    trial_number: int,
    raw: Mapping[str, object],
    rule: Mapping[str, object],
    failures: list[str],
    optuna_state: str,
    trial_state: str,
    result: Mapping[str, object] | None,
    baseline: Mapping[str, object],
    data_identity: Mapping[str, object],
    runtime_identity: str,
    exception: BaseException | None = None,
) -> dict[str, object]:
    primary = dict(result["primary"]) if result is not None else {}
    stress = dict(result["stress"]) if result is not None else {}
    baseline_primary = dict(baseline["primary"])
    baseline_stress = dict(baseline["stress"])
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
        "prototype_id": prototype_id,
        "study_seed": seed,
        "trial_number": trial_number,
        "raw_parameters_json": _json(raw),
        "effective_parameters_json": _json(rule),
        "constraint_status": "PASS" if not failures else "INVALID",
        "constraint_failures_json": _json(failures),
        "optuna_state": optuna_state,
        "trial_state": trial_state,
        "exception_type": "" if exception is None else type(exception).__name__,
        "exception_message": "" if exception is None else str(exception),
        "evaluation_start": str(data_identity["evaluation_start"]),
        "evaluation_end": str(data_identity["evaluation_end"]),
        "primary_strategy_metrics_json": _json(primary),
        "stress_strategy_metrics_json": _json(stress),
        "primary_buyhold_metrics_json": _json(baseline_primary),
        "stress_buyhold_metrics_json": _json(baseline_stress),
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
        raise ValueError("EX36 must explicitly declare formal return search")
    prohibited = (
        "selects_winner",
        "candidate_generation",
        "reads_sealed_validation",
        "mutates_catalog",
        "mutates_platform",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in prohibited):
        raise ValueError("EX36 scope exceeds formal development-pool search")

    sources = dict(protocol["sources"])
    frozen_paths = {
        repo / sources["ex35_manifest_path"]: sources["ex35_manifest_sha256"],
        repo / sources["ex34_manifest_path"]: sources["ex34_manifest_sha256"],
        repo / sources["ex33_manifest_path"]: sources["ex33_manifest_sha256"],
        repo / sources["ex32_manifest_path"]: sources["ex32_manifest_sha256"],
        repo / sources["ex31_manifest_path"]: sources["ex31_manifest_sha256"],
        repo / sources["ex30_manifest_path"]: sources["ex30_manifest_sha256"],
        repo / sources["ex29_manifest_path"]: sources["ex29_manifest_sha256"],
        repo / sources["ex28_manifest_path"]: sources["ex28_manifest_sha256"],
        repo / sources["ex27_manifest_path"]: sources["ex27_manifest_sha256"],
        repo / sources["ex26_manifest_path"]: sources["ex26_manifest_sha256"],
        repo / sources["ex25_manifest_path"]: sources["ex25_manifest_sha256"],
        repo / sources["ex16_manifest_path"]: sources["ex16_manifest_sha256"],
        repo / sources["ex16_feature_panel_path"]: sources[
            "ex16_feature_panel_sha256"
        ],
        repo / sources["materials_path"]: sources["materials_sha256"],
        repo / sources["execution_manifest_path"]: sources[
            "execution_manifest_sha256"
        ],
        repo / "packages/strategy_runtime/src/strategy_runtime/strategy.py": sources[
            "srt_strategy_sha256"
        ],
        repo
        / "packages/strategy_runtime/src/strategy_runtime/execution_planner.py": sources[
            "srt_planner_sha256"
        ],
        repo
        / "packages/trading_execution_engine/src/trading_execution_engine/historical.py": sources[
            "txe_historical_sha256"
        ],
    }
    for path, expected in frozen_paths.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen search source differs: {path}")
    ex35_archive = repo / str(sources["ex35_manifest_path"]).replace(
        "/experiment_manifest.json", ""
    )
    ex34_archive = repo / str(sources["ex34_manifest_path"]).replace(
        "/experiment_manifest.json", ""
    )
    ex33_archive = repo / str(sources["ex33_manifest_path"]).replace(
        "/experiment_manifest.json", ""
    )
    ex32_archive = repo / str(sources["ex32_manifest_path"]).replace(
        "/experiment_manifest.json", ""
    )
    ex31_archive = repo / str(sources["ex31_manifest_path"]).replace(
        "/experiment_manifest.json", ""
    )
    ex30_archive = repo / str(sources["ex30_manifest_path"]).replace(
        "/experiment_manifest.json", ""
    )
    ex29_archive = repo / str(sources["ex29_manifest_path"]).replace(
        "/experiment_manifest.json", ""
    )
    ex28_archive = repo / str(sources["ex28_manifest_path"]).replace(
        "/experiment_manifest.json", ""
    )
    ex27_archive = repo / str(sources["ex27_manifest_path"]).replace(
        "/experiment_manifest.json", ""
    )
    ex26_archive = repo / str(sources["ex26_manifest_path"]).replace(
        "/experiment_manifest.json", ""
    )
    ex25_archive = repo / str(sources["ex25_manifest_path"]).replace(
        "/experiment_manifest.json", ""
    )
    ex16_archive = repo / str(sources["ex16_manifest_path"]).replace(
        "/experiment_manifest.json", ""
    )
    validate_experiment_archive(ex35_archive)
    validate_experiment_archive(ex34_archive)
    validate_experiment_archive(ex33_archive)
    validate_experiment_archive(ex32_archive)
    validate_experiment_archive(ex31_archive)
    validate_experiment_archive(ex30_archive)
    validate_experiment_archive(ex29_archive)
    validate_experiment_archive(ex28_archive)
    validate_experiment_archive(ex27_archive)
    validate_experiment_archive(ex26_archive)
    validate_experiment_archive(ex25_archive)
    validate_experiment_archive(ex16_archive)

    search_contract = dict(protocol["search_execution"])
    if (
        search_contract["storage"] != "optuna.storages.InMemoryStorage"
        or int(search_contract["logical_workers"]) != 8
        or not bool(search_contract["studies_run_sequentially"])
    ):
        raise ValueError("EX36 in-memory, eight-core sequential execution differs")
    if optuna.__version__ != "4.9.0":
        raise ValueError("Optuna version differs from EX25")

    runtime_root = experiment / "runtime/strategy_runtime"
    runtime_path = runtime_root / "strategies/s008_prototypes.py"
    if _sha256(runtime_path) != sources["runtime_raw_sha256"]:
        raise ValueError("EX36 runtime raw source differs")
    runtime_identity = implementation_sha256(
        protocol["runtime"]["source_files"], source_root=runtime_root
    )
    if runtime_identity != sources["runtime_implementation_sha256"]:
        raise ValueError("EX36 runtime implementation identity differs")
    runtime = _load_runtime(runtime_path, "s008_ex36_main_runtime")

    ex25_protocol = _read(
        repo / "experiments/S008/20260923_S008_EX25/artifacts/protocol.json"
    )
    registry_path = repo / ex25_protocol["sources"]["ex19_registry_path"]
    if _sha256(registry_path) != ex25_protocol["sources"]["ex19_registry_sha256"]:
        raise ValueError("EX19 prototype registry differs from EX25")
    registry = _read(registry_path)
    registered = {item["prototype_id"]: item for item in registry["prototypes"]}
    prototype_order = list(search_contract["prototype_order"])
    if prototype_order != list(registered):
        raise ValueError("EX36 prototype order differs from EX19/EX25")

    execution_daily = _load_execution_daily(
        repo,
        repo / sources["execution_manifest_path"],
        pd.Timestamp(protocol["development_cutoff"]),
    )
    tmp_parent = (repo / ".tmp").resolve()
    tmp_parent.mkdir(exist_ok=True)
    prepared_root = (tmp_parent / "S008_EX36_prepared").resolve()
    if prepared_root.parent != tmp_parent:
        raise ValueError("unsafe EX36 temporary directory")
    if prepared_root.exists():
        raise ValueError("EX36 temporary directory already exists; refusing an implicit resume")

    full_window = TradableWindow(
        pd.Timestamp(protocol["development_start"]).date(),
        pd.Timestamp(protocol["development_cutoff"]).date(),
    )
    anchor_candidate = _candidate(
        runtime_root,
        runtime_identity,
        "S008-P01-COMPOSITE-GATE",
        "EX36ANCHOR",
        _anchor_rule(),
    )
    base_instance = StrategyRuntime().create(
        StrategyInit(anchor_candidate, full_window, prepared_root)
    )
    load_dotenv(repo / ".env", override=False)
    if not os.environ.get("TUSHARE_TOKEN"):
        raise ValueError("Tushare credential remains unavailable after loading repository .env")
    prepared_summary = base_instance.prepare_data()
    base_data = base_instance._prepared()
    input_frames = {
        name: result.dataframe.copy()
        for name, result in base_data._inputs.results.items()
    }
    component_panel = runtime.materialize_components(input_frames)
    normalized = runtime.normalize_components(component_panel, 252, 126)

    ex16 = pd.read_csv(
        repo / sources["ex16_feature_panel_path"], parse_dates=["Date"]
    ).set_index("Date")
    if not component_panel.index.isin(ex16.index).all():
        raise ValueError("runtime component signal domain exceeds EX16")
    runtime_development = component_panel.loc[:, list(_COMPONENTS)]
    reference_development = ex16.reindex(component_panel.index).loc[:, list(_COMPONENTS)]
    component_differences: dict[str, float] = {}
    for name in _COMPONENTS:
        left = runtime_development[name].to_numpy(dtype=float)
        right = reference_development[name].to_numpy(dtype=float)
        if not np.array_equal(np.isnan(left), np.isnan(right)):
            raise ValueError(f"runtime and EX16 null masks differ: {name}")
        finite = np.isfinite(left) & np.isfinite(right)
        difference = float(np.max(np.abs(left[finite] - right[finite]))) if finite.any() else 0.0
        component_differences[name] = difference
        if difference > 1e-12:
            raise ValueError(f"runtime and EX16 component values differ: {name} {difference}")

    valid = normalized.loc[
        normalized.index.to_series().between(
            protocol["development_start"], protocol["development_cutoff"]
        )
    ].notna().all(axis=1)
    if not valid.any():
        raise ValueError("no development session has all normalized components")
    evaluation_start = pd.Timestamp(valid.index[valid][0]).normalize()
    evaluation_end = pd.Timestamp(protocol["development_cutoff"]).normalize()
    execution_index = pd.DatetimeIndex(execution_daily["dt"])
    evaluation_dates = execution_index[
        (execution_index >= evaluation_start) & (execution_index <= evaluation_end)
    ]
    if len(evaluation_dates) < 2 or evaluation_dates[0] != evaluation_start:
        raise ValueError("evaluation boundary differs from execution calendar")
    positions = execution_index.get_indexer(evaluation_dates)
    if (positions <= 0).any():
        raise ValueError("evaluation session has no prior signal session")
    signal_dates = {
        trading.date(): execution_index[position - 1].date()
        for trading, position in zip(evaluation_dates, positions)
    }
    signal_sessions = tuple(pd.Timestamp(item) for item in signal_dates.values())
    missing_signals = pd.DatetimeIndex(signal_sessions).difference(normalized.index)
    if not missing_signals.empty:
        raise ValueError("normalized component cache misses required signal sessions")

    data_identity = {
        "prepared_data_identity": prepared_summary.data_identity,
        "input_identities": dict(base_data.input_identities),
        "price_identities": dict(base_data.price_identities),
        "ex16_feature_panel_sha256": sources["ex16_feature_panel_sha256"],
        "execution_manifest_sha256": sources["execution_manifest_sha256"],
        "evaluation_start": evaluation_start.date().isoformat(),
        "evaluation_end": evaluation_end.date().isoformat(),
        "signal_session_count": len(signal_sessions),
    }
    bundle = {
        "runtime_path": str(runtime_path),
        "source_root": str(runtime_root),
        "runtime_identity": runtime_identity,
        "normalized": normalized,
        "adjusted_daily": base_data.adjusted_daily,
        "execution_daily": execution_daily,
        "input_identities": dict(base_data.input_identities),
        "price_identities": dict(base_data.price_identities),
        "base_data_identity": prepared_summary.data_identity,
        "evaluation_start": evaluation_start,
        "evaluation_end": evaluation_end,
        "signal_dates": signal_dates,
        "signal_sessions": signal_sessions,
        "worker_data_dir": str(tmp_parent / "S008_EX36_worker"),
    }
    _worker_init(bundle)

    anchor_history = _history_for_rule(
        "S008-P01-COMPOSITE-GATE", _anchor_rule()
    )
    full_anchor_history = base_instance.inspect_signals().reindex(
        pd.DatetimeIndex(signal_sessions)
    )
    pd.testing.assert_frame_equal(
        anchor_history.loc[:, full_anchor_history.columns],
        full_anchor_history,
        check_dtype=False,
        check_exact=False,
        atol=1e-12,
        rtol=1e-12,
    )
    full_executor = HistoricalExecutor(
        strategy_reference=base_instance.definition.release_id,
        symbol=base_instance.identity.symbol,
        execution_daily=execution_daily,
        execution_intraday=pd.DataFrame(columns=["dt", "high", "low"]),
        evaluation_start=pd.Timestamp(protocol["development_start"]),
        evaluation_end=evaluation_end,
        initial_cash=_INITIAL_CASH,
        execution_policy=base_instance.execution_policy,
        order_types=base_instance.definition.capabilities.order_types,
        account_id="EX36-anchor-full",
    )
    full_anchor_account = base_instance.run_window(executor=full_executor)
    accelerated_anchor = _run_account(anchor_candidate, anchor_history, 0.001)
    full_equity = full_anchor_account.account_daily.copy()
    full_equity["date"] = pd.to_datetime(full_equity["date"]).dt.normalize()
    full_equity = full_equity.loc[full_equity["date"].ge(evaluation_start), "equity"]
    accelerated_equity = accelerated_anchor.account_daily["equity"]
    if not np.allclose(
        full_equity.to_numpy(dtype=float),
        accelerated_equity.to_numpy(dtype=float),
        rtol=0.0,
        atol=1e-8,
    ):
        raise ValueError("accelerated account path differs from complete SRT/TXE anchor")

    buyhold_history = anchor_history.copy()
    buyhold_history["target_position"] = [
        int(pd.Timestamp(signal) >= evaluation_start) for signal in signal_sessions
    ]
    buyhold_candidate = _candidate(
        runtime_root,
        runtime_identity,
        "S008-P01-COMPOSITE-GATE",
        "EX36BUYHOLD",
        _anchor_rule(),
    )
    baseline_primary = _account_metrics(
        _run_account(buyhold_candidate, buyhold_history, 0.001)
    )
    baseline_stress = _account_metrics(
        _run_account(buyhold_candidate, buyhold_history, 0.003)
    )
    baseline = {"primary": baseline_primary, "stress": baseline_stress}

    _write(
        artifacts / "input_and_acceleration_evidence.json",
        {
            "schema_version": 1,
            "experiment_id": EXPERIMENT_ID,
            "data_identity": data_identity,
            "component_max_absolute_differences": component_differences,
            "all_component_values_match_ex16": True,
            "anchor_signal_history_matches_complete_srt": True,
            "anchor_account_equity_matches_complete_srt_txe": True,
            "runtime_implementation_sha256": runtime_identity,
            "logical_workers": 8,
            "native_threads_per_worker": 1,
        },
    )
    _write(artifacts / "buyhold_metrics.json", baseline)

    protocol_hash = _sha256(protocol_path)
    rows: list[dict[str, object]] = []
    study_summaries: list[dict[str, object]] = []
    ledger_path = artifacts / "search_trial_ledger.csv.gz"
    worker_count = int(search_contract["logical_workers"])
    batch_size = int(search_contract["evaluation_batch_size"])
    target_trials = int(search_contract["trials_per_prototype"])
    invalid_annual = float(search_contract["invalid_objectives"]["annualized_return"])
    invalid_drawdown = float(
        search_contract["invalid_objectives"]["maximum_drawdown_magnitude"]
    )
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    for prototype_ordinal, prototype_id in enumerate(prototype_order, start=1):
        seed = int(search_contract["seeds"][prototype_id])
        storage = optuna.storages.InMemoryStorage()
        sampler = optuna.samplers.NSGAIISampler(
            seed=seed,
            population_size=int(search_contract["sampler_population_size"]),
            constraints_func=lambda frozen: frozen.user_attrs.get(
                "constraints", [1.0, 1.0, 1.0]
            ),
        )
        study = optuna.create_study(
            study_name=f"{EXPERIMENT_ID}-{prototype_id}",
            directions=["maximize", "minimize"],
            storage=storage,
            sampler=sampler,
            pruner=optuna.pruners.NopPruner(),
        )
        if not isinstance(storage, optuna.storages.InMemoryStorage):
            raise ValueError("Optuna storage is not InMemoryStorage")
        prototype_started = time.perf_counter()
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(
            max_workers=worker_count,
            mp_context=context,
            initializer=_worker_init,
            initargs=(bundle,),
        ) as pool:
            for batch_start in range(0, target_trials, batch_size):
                batch: list[dict[str, object]] = []
                futures: dict[int, object] = {}
                for _ in range(batch_size):
                    trial = study.ask()
                    raw = _suggest(trial, registered[prototype_id]["search_space"])
                    rule, failures, constraints = _effective_rule(prototype_id, raw)
                    trial.set_user_attr("constraints", constraints)
                    item = {
                        "trial": trial,
                        "raw": raw,
                        "rule": rule,
                        "failures": failures,
                    }
                    batch.append(item)
                    if not failures:
                        task = {
                            "prototype_id": prototype_id,
                            "prototype_ordinal": prototype_ordinal,
                            "trial_number": trial.number,
                            "rule": rule,
                        }
                        futures[trial.number] = pool.submit(_evaluate_worker, task)

                for item in sorted(batch, key=lambda value: value["trial"].number):
                    trial = item["trial"]
                    raw = item["raw"]
                    rule = item["rule"]
                    failures = item["failures"]
                    if failures:
                        study.tell(trial, values=(invalid_annual, invalid_drawdown))
                        row = _trial_row(
                            protocol_hash=protocol_hash,
                            prototype_id=prototype_id,
                            seed=seed,
                            trial_number=trial.number,
                            raw=raw,
                            rule=rule,
                            failures=failures,
                            optuna_state="COMPLETE",
                            trial_state="CONSTRAINT_INVALID",
                            result=None,
                            baseline=baseline,
                            data_identity=data_identity,
                            runtime_identity=runtime_identity,
                        )
                    else:
                        try:
                            result = futures[trial.number].result()
                            annualized = float(
                                result["primary"]["annualized_return"]
                            )
                            drawdown = float(
                                result["primary"]["maximum_drawdown_magnitude"]
                            )
                            if not np.isfinite(annualized) or not np.isfinite(drawdown):
                                study.tell(
                                    trial,
                                    values=(invalid_annual, invalid_drawdown),
                                )
                                trial_state = "METRIC_INVALID"
                                result_for_row = result
                            else:
                                study.tell(trial, values=(annualized, drawdown))
                                trial_state = "COMPLETE"
                                result_for_row = result
                            row = _trial_row(
                                protocol_hash=protocol_hash,
                                prototype_id=prototype_id,
                                seed=seed,
                                trial_number=trial.number,
                                raw=raw,
                                rule=rule,
                                failures=[],
                                optuna_state="COMPLETE",
                                trial_state=trial_state,
                                result=result_for_row,
                                baseline=baseline,
                                data_identity=data_identity,
                                runtime_identity=runtime_identity,
                            )
                        except BaseException as exc:
                            study.tell(trial, state=TrialState.FAIL)
                            row = _trial_row(
                                protocol_hash=protocol_hash,
                                prototype_id=prototype_id,
                                seed=seed,
                                trial_number=trial.number,
                                raw=raw,
                                rule=rule,
                                failures=[],
                                optuna_state="FAIL",
                                trial_state="EXECUTION_FAILED",
                                result=None,
                                baseline=baseline,
                                data_identity=data_identity,
                                runtime_identity=runtime_identity,
                                exception=exc,
                            )
                            rows.append(row)
                            _checkpoint(ledger_path, rows)
                            raise RuntimeError(
                                f"EX36 execution failed: {prototype_id} trial {trial.number}"
                            ) from exc
                    rows.append(row)
                _checkpoint(ledger_path, rows)
                completed = batch_start + batch_size
                print(
                    f"{prototype_id}: {completed}/{target_trials} trials; "
                    f"qualified={sum(bool(row['hard_qualified']) for row in rows if row['prototype_id'] == prototype_id)}",
                    flush=True,
                )
        prototype_rows = [row for row in rows if row["prototype_id"] == prototype_id]
        study_summaries.append(
            {
                "prototype_id": prototype_id,
                "seed": seed,
                "storage": type(storage).__name__,
                "attempted_trials": len(prototype_rows),
                "complete_trials": sum(
                    row["trial_state"] == "COMPLETE" for row in prototype_rows
                ),
                "constraint_invalid_trials": sum(
                    row["trial_state"] == "CONSTRAINT_INVALID"
                    for row in prototype_rows
                ),
                "metric_invalid_trials": sum(
                    row["trial_state"] == "METRIC_INVALID" for row in prototype_rows
                ),
                "hard_qualified_trials": sum(
                    bool(row["hard_qualified"]) for row in prototype_rows
                ),
                "runtime_seconds": round(time.perf_counter() - prototype_started, 3),
            }
        )

    ledger = pd.DataFrame(rows).sort_values(["prototype_id", "trial_number"])
    if len(ledger) != int(search_contract["total_trials"]):
        raise ValueError("EX36 trial ledger count differs from frozen budget")
    if ledger["trial_state"].eq("EXECUTION_FAILED").any():
        raise ValueError("EX36 contains an execution failure")
    qualified = ledger.loc[ledger["hard_qualified"].eq(True)].copy()
    pareto = qualified.loc[_pareto(qualified)].copy() if len(qualified) else qualified.copy()
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    qualified.to_csv(
        artifacts / "hard_qualified_trials.csv.gz",
        index=False,
        compression=compression,
        lineterminator="\n",
    )
    pareto.to_csv(
        artifacts / "hard_qualified_pareto.csv.gz",
        index=False,
        compression=compression,
        lineterminator="\n",
    )
    decision = (
        "PROCEED_TO_PARAMETER_PLATFORM_AUDIT"
        if len(qualified)
        else "STOP_AND_REVIEW_NO_FEASIBLE_PROTOTYPE"
    )
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": decision,
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "evaluation_start": evaluation_start.date().isoformat(),
        "evaluation_end": evaluation_end.date().isoformat(),
        "logical_workers": worker_count,
        "studies_run_sequentially": True,
        "storage": "InMemoryStorage",
        "study_summaries": study_summaries,
        "total_attempted_trials": int(len(ledger)),
        "total_complete_trials": int(ledger["trial_state"].eq("COMPLETE").sum()),
        "total_constraint_invalid_trials": int(
            ledger["trial_state"].eq("CONSTRAINT_INVALID").sum()
        ),
        "total_metric_invalid_trials": int(
            ledger["trial_state"].eq("METRIC_INVALID").sum()
        ),
        "total_hard_qualified_trials": int(len(qualified)),
        "hard_qualified_pareto_trials": int(len(pareto)),
        "unique_hard_qualified_behaviors": int(
            qualified["behavior_sha256"].nunique()
        )
        if len(qualified)
        else 0,
        "buyhold_primary": baseline_primary,
        "buyhold_stress": baseline_stress,
        "winner_selected": False,
        "candidate_created": False,
        "sealed_validation_read": False,
        "catalog_mutated": False,
        "platform_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "search_evidence.json", evidence)

    if prepared_root.exists():
        shutil.rmtree(prepared_root)
    (experiment / "03_execution.md").write_text(
        "# S008 EX36 执行记录\n\n"
        f"按P01、P02、P03顺序完成三套独立InMemoryStorage study，共{len(ledger)}次trial；"
        f"每个study内部使用8个工作进程，完整有效评价{evidence['total_complete_trials']}次，"
        f"显式约束无效{evidence['total_constraint_invalid_trials']}次，指标无效"
        f"{evidence['total_metric_invalid_trials']}次。完整trial账本已归档。\n\n"
        "搜索前的13组件、信号历史和账户权益等价门全部通过；未读取封存池、选择最终参数或创建候选。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX36 实验结论\n\n"
        f"机器裁决：`{decision}`。主成本下共有{len(qualified)}个trial同时满足年化收益与最大回撤"
        f"两项硬门，覆盖{evidence['unique_hard_qualified_behaviors']}种独立目标仓位行为；其中"
        f"非支配合格点{len(pareto)}个。\n\n"
        "本实验只证明冻结搜索空间中是否存在硬门可行点，不选择冠军参数；若存在可行点，下一步"
        "必须检查连通平台、局部可行率和代表点稳定性。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S008",
            "credential_id": "SGC-S008-001",
            "symbol": "518880.SH",
            "development_cutoff": "2024-12-31",
            "decision": decision,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
