"""Run complete candidate payloads through Trader's existing backtest semantics."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from typing import Any

from joblib import Parallel, delayed, parallel_config
import numpy as np
import pandas as pd
from strategy_evaluator import EvaluationProtocol, MetricObservation, MetricStatus

from .audit import audit_candidate_evaluation, audit_no_lookahead
from .research_backtest import run_period_backtests
from .baseline_execution import apply_resolved_baseline
from .baselines import ExecutionSpec, resolve_strategy_payload
from .backtesting.datasets import load_replay_data
from .execution_policy import ExecutionSimulation, entry_limit_series, simulate_limit_policy
from .factors import generate_factor_frame
from .four_layer import normalized_signal_factors
from .range_diagnostics import range_cycle_objectives
from .regime_weight import classify_regimes, lagged_efficiency_ratio
from .strategy_metrics import closed_trade_ledger
from .strategy_runtime import apply_resolved_strategy

METRIC_SEMANTICS_VERSION = "candidate-metrics-v1"


@dataclass(frozen=True)
class CandidateEvaluationContext:
    repository: Any
    symbol: str
    asset_type: str
    periods: tuple[tuple[str, tuple[pd.Timestamp, pd.Timestamp]], ...]
    fee_rate: float = 0.0005
    init_cash: float = 1_000_000.0
    workers: int = 1


@dataclass(frozen=True)
class EvaluationWorkspace:
    data: Any
    execution_daily: pd.DataFrame
    execution_intraday: pd.DataFrame
    factor_frame: pd.DataFrame
    daily_close: pd.Series
    periods: dict[str, tuple[pd.Timestamp, pd.Timestamp]]


@dataclass(frozen=True)
class BehaviorMember:
    candidate_id: str
    target_position: pd.Series
    scores: pd.Series
    events: pd.DataFrame
    regimes: pd.Series | None


@dataclass(frozen=True)
class BehaviorTask:
    key: str
    members: tuple[BehaviorMember, ...]
    execution: Any


@dataclass(frozen=True)
class BehaviorResult:
    key: str
    observations: tuple[MetricObservation, ...]


def _scenario_settings(scenario: str, base_fee_rate: float) -> tuple[float, int]:
    if scenario == "standard":
        return base_fee_rate, 0
    if scenario.startswith("fee_x"):
        return base_fee_rate * float(scenario.removeprefix("fee_x")), 0
    if scenario.startswith("slippage_") and scenario.endswith("bp"):
        return base_fee_rate, int(scenario.removeprefix("slippage_").removesuffix("bp"))
    raise ValueError(f"unsupported stress scenario: {scenario}")


def prepare_evaluation_workspace(
    context: CandidateEvaluationContext,
    protocol: EvaluationProtocol,
) -> EvaluationWorkspace:
    replay_data = load_replay_data(
        context.repository,
        "research",
        context.symbol,
        context.asset_type,
        protocol.development_cutoff,
    )
    data = replay_data.adjusted
    factors = generate_factor_frame(data)
    daily_close = pd.Series(
        data.daily["close"].astype(float).to_numpy(),
        index=pd.DatetimeIndex(pd.to_datetime(data.daily["dt"]), name="dt"),
        name="close",
    )
    return EvaluationWorkspace(
        data,
        replay_data.execution_daily,
        replay_data.execution_intraday,
        factors.frame,
        daily_close,
        dict(context.periods),
    )


def complete_execution_results(
    daily: pd.DataFrame,
    intraday: pd.DataFrame,
    target_position: pd.Series,
    periods: dict[str, tuple[pd.Timestamp, pd.Timestamp]],
    execution: ExecutionSpec,
    *,
    fee_rate: float,
    init_cash: float,
    slippage_bp: int = 0,
) -> dict[str, ExecutionSimulation]:
    """Run research windows with unadjusted execution-price semantics."""
    daily_dates = pd.DatetimeIndex(pd.to_datetime(daily["dt"]), name="dt")
    limits = entry_limit_series(
        daily,
        "fixed",
        execution.entry_limit_parameter,
        tick=execution.instrument.price_tick,
    )
    output: dict[str, ExecutionSimulation] = {}
    for name, (start, end) in periods.items():
        prior = daily_dates[daily_dates < start]
        if prior.empty:
            raise ValueError(f"execution-policy window {name} has no prior signal session")
        simulation_start = prior[-1]
        period_daily = daily.loc[
            pd.Series(daily_dates, index=daily.index).between(simulation_start, end)
        ].copy()
        intraday_dates = pd.to_datetime(intraday["dt"]).dt.normalize()
        period_intraday = intraday.loc[
            intraday_dates.between(simulation_start.normalize(), end.normalize())
        ].copy()
        period_index = pd.DatetimeIndex(pd.to_datetime(period_daily["dt"]), name="dt")
        simulation = simulate_limit_policy(
            period_daily,
            period_intraday,
            target_position.reindex(period_index),
            limits.reindex(period_index),
            fee_rate=fee_rate,
            init_cash=init_cash,
            diagnostic_quantity=execution.instrument.maximum_order_quantity,
            lot_size=execution.instrument.lot_size,
            fill_on_equal_touch=False,
            slippage_bp=slippage_bp,
        )
        execution_dates = pd.to_datetime(simulation.orders["execution_date"])
        orders = simulation.orders.loc[
            execution_dates.dt.normalize().between(start.normalize(), end.normalize())
        ].copy().reset_index(drop=True)
        output[name] = ExecutionSimulation(
            simulation.equity.loc[start:end].copy(),
            orders,
            simulation.daily_state.loc[start:end].copy(),
            simulation.cycles.copy(),
            {},
        )
    return output


def _contiguous_chunks(items: tuple[Any, ...], workers: int) -> tuple[tuple[Any, ...], ...]:
    if not items:
        return ()
    count = min(max(int(workers), 1), len(items))
    quotient, remainder = divmod(len(items), count)
    chunks: list[tuple[Any, ...]] = []
    start = 0
    for index in range(count):
        size = quotient + (1 if index < remainder else 0)
        chunks.append(items[start : start + size])
        start += size
    return tuple(chunks)


def _run_behavior_chunk(
    workspace: EvaluationWorkspace,
    chunk: tuple[BehaviorTask, ...],
    tier: str,
    scenario: str,
    fee_rate: float,
    init_cash: float,
    slippage_bp: int = 0,
) -> tuple[BehaviorResult, ...]:
    output: list[BehaviorResult] = []
    daily_dates = pd.DatetimeIndex(pd.to_datetime(workspace.data.daily["dt"]))
    for task in chunk:
        representative = task.members[0]
        results = run_period_backtests(
            workspace.data.daily,
            representative.target_position,
            workspace.periods,
            fee_rate=fee_rate,
            init_cash=init_cash,
        )
        results = {
            name: replace(result, portfolio=None)
            for name, result in results.items()
        }
        execution_results = {}
        if tier in {"FORMAL", "STRESS"} and task.execution is not None:
            execution_results = complete_execution_results(
                workspace.execution_daily,
                workspace.execution_intraday,
                representative.target_position,
                workspace.periods,
                task.execution,
                fee_rate=fee_rate,
                init_cash=init_cash,
                slippage_bp=slippage_bp,
            )
        observations: list[MetricObservation] = []
        for member in task.members:
            audit_candidate_evaluation(
                results, member.target_position, member.events, member.scores,
            )
            for window, result in results.items():
                effective = execution_results.get(window, result)
                extra = () if member.regimes is None else range_cycle_objectives(
                    effective.orders, member.regimes, daily_dates,
                )
                observations.append(_observation(
                    member.candidate_id,
                    window,
                    tier,
                    scenario,
                    effective.equity,
                    effective.orders,
                    init_cash,
                    extra,
                ))
        output.append(BehaviorResult(task.key, tuple(observations)))
    return tuple(output)


def _evaluate_behavior_tasks(
    workspace: EvaluationWorkspace,
    tasks: tuple[BehaviorTask, ...],
    tier: str,
    scenario: str,
    fee_rate: float,
    init_cash: float,
    workers: int,
    slippage_bp: int = 0,
) -> tuple[BehaviorResult, ...]:
    chunks = _contiguous_chunks(tasks, workers)
    if workers == 1 or len(tasks) < 32:
        return tuple(
            item
            for chunk in chunks
            for item in _run_behavior_chunk(
                workspace, chunk, tier, scenario, fee_rate, init_cash, slippage_bp,
            )
        )
    with parallel_config(backend="loky", inner_max_num_threads=1):
        pieces = Parallel(n_jobs=min(workers, len(chunks)), max_nbytes="1M", mmap_mode="r")(
            delayed(_run_behavior_chunk)(
                workspace, chunk, tier, scenario, fee_rate, init_cash, slippage_bp,
            )
            for chunk in chunks
        )
    return tuple(item for piece in pieces for item in piece)


def _profit_factor(orders: pd.DataFrame) -> tuple[float | None, MetricStatus, int]:
    ledger = closed_trade_ledger(orders)
    count = len(ledger)
    if count == 0:
        return None, MetricStatus.NO_CLOSED_TRADES, 0
    returns = ledger["net_return"].astype(float)
    wins, losses = returns[returns > 0], returns[returns < 0]
    if wins.empty:
        return None, MetricStatus.NO_WINS, count
    if losses.empty:
        return None, MetricStatus.NO_LOSSES, count
    return float(wins.sum() / abs(losses.sum())), MetricStatus.VALID, count


def _observation(candidate_id: str, window: str, tier: str, scenario: str, equity: pd.Series, orders: pd.DataFrame, init_cash: float, extra_objectives: tuple[tuple[str, float], ...] = ()) -> MetricObservation:
    total_return = float(equity.iloc[-1] / init_cash - 1.0)
    net_cagr = float((equity.iloc[-1] / init_cash) ** (252.0 / len(equity)) - 1.0)
    max_drawdown = float(equity.div(equity.cummax()).sub(1.0).min())
    calmar = net_cagr / abs(max_drawdown) if abs(max_drawdown) > 1e-12 else None
    pf, pf_status, closed_trades = _profit_factor(orders)
    if pf_status is MetricStatus.VALID and closed_trades < 10:
        pf_status = MetricStatus.LOW_SAMPLE
    if orders.empty:
        turnover = 0.0
        cost_drag = 0.0
    else:
        turnover = float((orders["size"].astype(float) * orders["price"].astype(float)).abs().sum() / init_cash)
        cost_drag = float(orders["fees"].astype(float).sum() / init_cash)
    return MetricObservation(
        candidate_id, window, scenario, tier, net_cagr, total_return, max_drawdown,
        calmar, MetricStatus.VALID if calmar is not None and np.isfinite(calmar) else MetricStatus.UNAVAILABLE,
        pf, pf_status, closed_trades, turnover, cost_drag,
        (("net_cagr", net_cagr), ("total_return", total_return), (f"{window}_return", total_return), *extra_objectives),
    )


def _behavior_key(
    target: pd.Series,
    execution_policy_hash: str,
    tier: str,
    scenario: str,
    fee_rate: float,
) -> str:
    digest = hashlib.sha256(np.ascontiguousarray(target.astype(float).to_numpy()).tobytes())
    digest.update(json.dumps(
        [execution_policy_hash, tier, scenario, fee_rate],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8"))
    return digest.hexdigest()


def _evaluate_candidate_payloads_reference(
    context: CandidateEvaluationContext,
    protocol: EvaluationProtocol,
    candidates: tuple[dict[str, object], ...],
    candidate_ids: tuple[str, ...],
    tier: str,
    scenarios: tuple[str, ...] = ("standard",),
) -> tuple[MetricObservation, ...]:
    selected = {str(item["candidate_id"]): item for item in candidates if str(item["candidate_id"]) in candidate_ids}
    missing = set(candidate_ids) - set(selected)
    if missing:
        raise ValueError(f"candidate payloads missing: {sorted(missing)}")
    replay_data = load_replay_data(
        context.repository,
        "research",
        context.symbol,
        context.asset_type,
        protocol.development_cutoff,
    )
    data = replay_data.adjusted
    factors = generate_factor_frame(data)
    daily_close = pd.Series(data.daily["close"].astype(float).to_numpy(), index=pd.DatetimeIndex(pd.to_datetime(data.daily["dt"]), name="dt"), name="close")
    periods = dict(context.periods)
    output: list[MetricObservation] = []
    for candidate_id in candidate_ids:
        item = selected[candidate_id]
        strategy_payload = item.get("strategy_payload")
        if not isinstance(strategy_payload, dict):
            raise ValueError(f"candidate {candidate_id} has no complete strategy_payload")
        baseline = resolve_strategy_payload(
            context.repository.baseline_root, strategy_payload,
            release_id=candidate_id, release_hash=str(item.get("strategy_hash", item.get("candidate_hash", ""))), symbol=context.symbol,
            repository_root=context.repository.root,
        )
        if baseline.strategy == "czsc_event_hold":
            applied = apply_resolved_strategy(data, baseline)
        else:
            applied = apply_resolved_baseline(factors.frame, baseline, daily_close=daily_close)
        regimes = None
        if baseline.strategy == "czsc_regime_weight":
            regimes = classify_regimes(
                lagged_efficiency_ratio(daily_close.reindex(applied.target_position.index), baseline.er_lookback),
                baseline.er_threshold,
            )
        factor_output = factors.frame.copy()
        factor_output.insert(0, "target_position", applied.target_position)
        factor_output.insert(1, "factor_score", applied.scores)
        for scenario in scenarios:
            if scenario != "standard" and tier != "STRESS":
                raise ValueError("non-standard scenarios require STRESS tier")
            fee_rate, slippage_bp = _scenario_settings(scenario, context.fee_rate)
            results = run_period_backtests(
                data.daily, applied.target_position, periods, fee_rate=fee_rate, init_cash=context.init_cash,
                factor_events=applied.events, factor_frame=factor_output,
            )
            for result in results.values():
                audit_no_lookahead(result.orders, result.factor_events, applied.target_position, factor_output)
            execution_results = {}
            if tier in {"FORMAL", "STRESS"} and baseline.execution is not None:
                execution_results = complete_execution_results(
                    replay_data.execution_daily,
                    replay_data.execution_intraday,
                    applied.target_position,
                    periods,
                    baseline.execution,
                    fee_rate=fee_rate, init_cash=context.init_cash,
                    slippage_bp=slippage_bp,
                )
            for window, result in results.items():
                effective = execution_results.get(window, result)
                extra = () if regimes is None else range_cycle_objectives(
                    effective.orders, regimes, pd.DatetimeIndex(pd.to_datetime(data.daily["dt"])),
                )
                output.append(_observation(candidate_id, window, tier, scenario, effective.equity, effective.orders, context.init_cash, extra))
    return tuple(output)


def evaluate_candidate_payloads(
    context: CandidateEvaluationContext,
    protocol: EvaluationProtocol,
    candidates: tuple[dict[str, object], ...],
    candidate_ids: tuple[str, ...],
    tier: str,
    scenarios: tuple[str, ...] = ("standard",),
) -> tuple[MetricObservation, ...]:
    selected = {str(item["candidate_id"]): item for item in candidates if str(item["candidate_id"]) in candidate_ids}
    missing = set(candidate_ids) - set(selected)
    if missing:
        raise ValueError(f"candidate payloads missing: {sorted(missing)}")
    workspace = prepare_evaluation_workspace(context, protocol)
    resolved: dict[str, Any] = {}
    for candidate_id in candidate_ids:
        item = selected[candidate_id]
        strategy_payload = item.get("strategy_payload")
        if not isinstance(strategy_payload, dict):
            raise ValueError(f"candidate {candidate_id} has no complete strategy_payload")
        resolved[candidate_id] = resolve_strategy_payload(
            context.repository.baseline_root,
            strategy_payload,
            release_id=candidate_id,
            release_hash=str(item.get("strategy_hash", item.get("candidate_hash", ""))),
            symbol=context.symbol,
            repository_root=context.repository.root,
        )

    factor_cache: dict[tuple[str, ...], pd.DataFrame] = {}
    regime_cache: dict[tuple[int, float], pd.Series] = {}
    applied_by_candidate: dict[str, Any] = {}
    regimes_by_candidate: dict[str, pd.Series | None] = {}
    for candidate_id in candidate_ids:
        baseline = resolved[candidate_id]
        normalized = None
        regimes = None
        if baseline.strategy in {"czsc_four_layer", "czsc_regime_weight"}:
            names = tuple(map(str, baseline.factor_names))
            normalized = factor_cache.get(names)
            if normalized is None:
                normalized = normalized_signal_factors(workspace.factor_frame.loc[:, list(names)])
                factor_cache[names] = normalized
        if baseline.strategy == "czsc_regime_weight":
            regime_key = (int(baseline.er_lookback), float(baseline.er_threshold))
            regimes = regime_cache.get(regime_key)
            if regimes is None:
                aligned_close = workspace.daily_close.reindex(workspace.factor_frame.index)
                regimes = classify_regimes(
                    lagged_efficiency_ratio(aligned_close, baseline.er_lookback),
                    baseline.er_threshold,
                )
                regime_cache[regime_key] = regimes
        if baseline.strategy == "czsc_event_hold":
            applied_by_candidate[candidate_id] = apply_resolved_strategy(
                workspace.data, baseline,
            )
        else:
            applied_by_candidate[candidate_id] = apply_resolved_baseline(
                workspace.factor_frame,
                baseline,
                daily_close=workspace.daily_close,
                normalized_factors=normalized,
                regimes=regimes,
            )
        regimes_by_candidate[candidate_id] = regimes

    observations: dict[tuple[str, str, str], MetricObservation] = {}
    for scenario in scenarios:
        if scenario != "standard" and tier != "STRESS":
            raise ValueError("non-standard scenarios require STRESS tier")
        fee_rate, slippage_bp = _scenario_settings(scenario, context.fee_rate)
        groups: dict[str, list[str]] = {}
        for candidate_id in candidate_ids:
            key = _behavior_key(
                applied_by_candidate[candidate_id].target_position,
                str(selected[candidate_id].get("execution_policy_hash", "")),
                tier,
                scenario,
                fee_rate,
            )
            groups.setdefault(key, []).append(candidate_id)
        tasks = tuple(
            BehaviorTask(
                key,
                tuple(
                    BehaviorMember(
                        candidate_id,
                        applied_by_candidate[candidate_id].target_position,
                        applied_by_candidate[candidate_id].scores,
                        applied_by_candidate[candidate_id].events,
                        regimes_by_candidate[candidate_id],
                    )
                    for candidate_id in members
                ),
                resolved[members[0]].execution,
            )
            for key, members in groups.items()
        )
        behavior_results = _evaluate_behavior_tasks(
            workspace, tasks, tier, scenario, fee_rate, context.init_cash, context.workers,
            slippage_bp,
        )
        for result in behavior_results:
            for observation in result.observations:
                observations[
                    (observation.candidate_id, observation.scenario_id, observation.window_id)
                ] = observation
    return tuple(
        observations[(candidate_id, scenario, window)]
        for candidate_id in candidate_ids
        for scenario in scenarios
        for window in workspace.periods
    )
