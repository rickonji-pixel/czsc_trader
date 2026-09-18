"""Recompute candidate metrics from SRT decisions and the TXE account ledger."""

from __future__ import annotations

from dataclasses import dataclass, replace
from concurrent.futures import ThreadPoolExecutor
from math import isfinite
from typing import Any

import numpy as np
import pandas as pd
from strategy_evaluator import EvaluationProtocol, MetricObservation, MetricStatus
from strategy_runtime import StrategyCandidate, StrategyLoader, canonical_sha256

from .backtesting.datasets import load_replay_data
from .backtesting.models import StrategyIdentity, StrategySnapshot
from .backtesting.srt_bridge import (
    build_srt_signal_replay, execution_intraday_frequencies, load_srt_strategy,
    replay_srt_account,
)

METRIC_SEMANTICS_VERSION = "candidate-srt-txe-v2"


@dataclass(frozen=True)
class CandidateEvaluationContext:
    repository: Any
    symbol: str
    asset_type: str
    periods: tuple[tuple[str, tuple[pd.Timestamp, pd.Timestamp]], ...]
    fee_rate: float = 0.0005
    init_cash: float = 1_000_000.0
    workers: int = 1
    frequency_window_days: int = 60
    family_id: str = ""


@dataclass(frozen=True)
class EvaluationWorkspace:
    replay_data: Any
    periods: dict[str, tuple[pd.Timestamp, pd.Timestamp]]


def _scenario_settings(scenario: str, base_fee_rate: float) -> tuple[float, int]:
    if scenario == "standard":
        fee, slippage = base_fee_rate, 0
    elif scenario.startswith("fee_x"):
        fee, slippage = base_fee_rate * float(scenario.removeprefix("fee_x")), 0
    elif scenario.startswith("total_cost_") and scenario.endswith("bp"):
        fee, slippage = float(scenario.removeprefix("total_cost_").removesuffix("bp")) / 10_000, 0
    elif scenario.startswith("slippage_") and scenario.endswith("bp"):
        fee, slippage = base_fee_rate, int(scenario.removeprefix("slippage_").removesuffix("bp"))
    else:
        raise ValueError(f"unsupported stress scenario: {scenario}")
    if not isfinite(fee) or not 0 <= fee < 1 or slippage < 0:
        raise ValueError(f"invalid cost scenario: {scenario}")
    return fee, slippage


def prepare_evaluation_workspace(
    context: CandidateEvaluationContext, protocol: EvaluationProtocol, *,
    include_five_minute: bool = False,
) -> EvaluationWorkspace:
    options = {"include_five_minute": True} if include_five_minute else {}
    replay = load_replay_data(
        context.repository, "research", context.symbol, context.asset_type,
        pd.Timestamp(protocol.development_cutoff).date(), **options,
    )
    cutoff = pd.Timestamp(protocol.development_cutoff).normalize()
    dates = pd.DatetimeIndex(pd.to_datetime(replay.adjusted.daily["dt"])).normalize()
    execution_dates = pd.DatetimeIndex(pd.to_datetime(replay.execution_daily["dt"])).normalize()
    for label, index in (("research market", dates), ("execution", execution_dates)):
        if index.empty or index.max() != cutoff:
            actual = None if index.empty else index.max().date().isoformat()
            raise ValueError(f"{label} data does not reach development cutoff: requested={cutoff.date()}, actual={actual}")
        if index.has_duplicates or not index.is_monotonic_increasing:
            raise ValueError(f"{label} sessions must be unique and increasing")
    if not dates.equals(execution_dates):
        raise ValueError("research and execution session calendars differ")
    periods = dict(context.periods)
    if not periods or len(periods) != len(context.periods):
        raise ValueError("evaluation windows must be non-empty and unique")
    for name, (start, end) in periods.items():
        if start != start.normalize() or end != end.normalize() or start > end:
            raise ValueError(f"invalid evaluation window: {name}")
        if start not in dates or end not in dates:
            raise ValueError(f"evaluation window {name} is not bounded by trading sessions")
        if end > cutoff:
            raise ValueError(f"evaluation window {name} exceeds development cutoff")
        if not (dates < start).any():
            raise ValueError(f"evaluation window {name} has no prior signal session")
    return EvaluationWorkspace(replay, periods)


def _snapshot(context: CandidateEvaluationContext, item: dict[str, object]):
    payload = item.get("strategy_payload")
    if not isinstance(payload, dict):
        raise ValueError(f"candidate {item['candidate_id']} has no complete strategy_payload")
    reference = str(item["candidate_id"])
    if item.get("is_incumbent") and "-v" in reference:
        release, strategy = load_srt_strategy(context.repository.root, reference, deployment_symbol=context.symbol)
        if canonical_sha256(payload) != canonical_sha256(release.payload):
            raise ValueError("incumbent manifest payload differs from the frozen release")
        identity, source_hash = StrategyIdentity("REGISTERED", reference, "evaluation"), release.release_hash
    else:
        family = str(item.get("strategy_id", context.family_id))
        candidate_id = reference.removeprefix(family + "-") if family else reference
        candidate = StrategyCandidate(family, candidate_id, payload)
        strategy = StrategyLoader().load_candidate(candidate)
        identity = StrategyIdentity("CANDIDATE", candidate.reference_id, "evaluation")
        source_hash = candidate.runtime_identity_sha256
    return StrategySnapshot(identity, source_hash, canonical_sha256(payload), payload, None), strategy


def prepare_candidate_replays(context, protocol, payloads, candidate_ids):
    """Load complete SRT identities, publications and window decisions once per call."""
    by_id = {str(item["candidate_id"]): item for item in payloads}
    if len(by_id) != len(payloads) or len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("duplicate candidate identities")
    if missing := set(candidate_ids) - set(by_id):
        raise ValueError(f"candidate payloads missing: {sorted(missing)}")
    loaded = {key: _snapshot(context, by_id[key]) for key in candidate_ids}
    workspace = prepare_evaluation_workspace(
        context, protocol,
        include_five_minute=any(execution_intraday_frequencies(strategy) for _, strategy in loaded.values()),
    )
    replays = {}
    for key, (snapshot, _) in loaded.items():
        replays[key] = {
            name: build_srt_signal_replay(
                snapshot=snapshot, replay_data=workspace.replay_data, start=start, end=end,
                repository_root=context.repository.root,
            )
            for name, (start, end) in workspace.periods.items()
        }
    return workspace, replays


def execute_candidate_replay(context, workspace, prepared, fee_rate):
    """Return the ledger and its effective-cost evidence without editing SRT."""
    strategy, signals = prepared
    result = replay_srt_account(
        strategy=strategy, signals=signals, replay_data=workspace.replay_data,
        initial_cash=context.init_cash, fee_rate_override=fee_rate,
    )
    support = dict(signals.support_data)
    policy = support["execution_policy"]
    settings = dict(policy["settings"])
    if policy["policy_type"] == "FROZEN_RULE":
        settings["capital"] = {**settings["capital"], "fee_rate": fee_rate}
    else:
        settings["one_way_cost"] = fee_rate
    support["execution_policy"] = {**policy, "settings": settings}
    support["evaluation_fee_rate"] = fee_rate
    return replace(signals, support_data=support), result


def _observation(context, candidate_id, window, tier, scenario, result, replay_data=None):
    equity = result.equity
    total = float(equity.iloc[-1] / context.init_cash - 1)
    cagr = float((1 + total) ** (252 / len(equity)) - 1)
    drawdown = float(equity.div(equity.cummax()).sub(1).min())
    calmar = cagr / abs(drawdown) if abs(drawdown) > 1e-12 else None
    closed = result.trades.loc[result.trades["status"].eq("CLOSED")]
    returns = closed["net_return"].astype(float)
    wins, losses = returns[returns > 0], returns[returns < 0]
    pf = None
    if closed.empty:
        status = MetricStatus.NO_CLOSED_TRADES
    elif wins.empty:
        status = MetricStatus.NO_WINS
    elif losses.empty:
        status = MetricStatus.NO_LOSSES
    else:
        pf = float(wins.sum() / abs(losses.sum()))
        status = MetricStatus.LOW_SAMPLE if len(closed) < 10 else MetricStatus.VALID
    fills = result.fills
    turnover = float((fills["quantity"] * fills["price"]).sum() / context.init_cash) if not fills.empty else 0.0
    cost = float(fills["fees"].sum() / context.init_cash) if not fills.empty else 0.0
    # Overlay core establishment is represented by opening account state, not an event fill.
    opening = result.account_daily.iloc[0]
    if int(opening["quantity_before"]) > 0:
        if replay_data is None:
            raise ValueError("opening holdings require an execution-price ledger")
        prior = replay_data.execution_daily.loc[replay_data.execution_daily["dt"] < opening["date"]]
        if prior.empty:
            raise ValueError("opening holdings have no pre-window execution price")
        gross = int(opening["quantity_before"]) * float(prior.iloc[-1]["close"])
        turnover += gross / context.init_cash
        cost += (context.init_cash - float(opening["cash_before"]) - gross) / context.init_cash
    count_days = context.frequency_window_days
    if type(count_days) is not int or count_days <= 0:
        raise ValueError("frequency window must be a positive integer")
    sessions = pd.DatetimeIndex(equity.index).normalize()
    exits = pd.DatetimeIndex(pd.to_datetime(closed["exit_date"])).normalize()
    counts = [float(((exits >= sessions[i-count_days+1]) & (exits <= sessions[i])).sum()) for i in range(count_days-1, len(sessions))]
    median, p10 = (float(np.median(counts)), float(np.quantile(counts, .1))) if counts else (None, None)
    return MetricObservation(
        candidate_id, window, scenario, tier, cagr, total, drawdown, calmar,
        MetricStatus.VALID if calmar is not None and isfinite(calmar) else MetricStatus.UNAVAILABLE,
        pf, status, len(closed), turnover, cost,
        (("net_cagr", cagr), ("total_return", total), (f"{window}_return", total)),
        count_days, median, p10,
    )


def evaluate_candidate_payloads(context, protocol, candidates, candidate_ids, tier, scenarios=("standard",)):
    if type(context.workers) is not int or context.workers < 1:
        raise ValueError("evaluation workers must be a positive integer")
    if tier not in {"SCREENING", "FORMAL", "STRESS"}:
        raise ValueError(f"unsupported evaluation tier: {tier}")
    if not scenarios or len(set(scenarios)) != len(scenarios):
        raise ValueError("cost scenarios must be non-empty and unique")
    costs = {}
    for scenario in scenarios:
        if scenario != "standard" and tier != "STRESS":
            raise ValueError("non-standard scenarios require STRESS tier")
        fee, slippage = _scenario_settings(scenario, context.fee_rate)
        if slippage or scenario.startswith("slippage_"):
            raise ValueError("TXE evaluation does not support price-slippage scenarios; declare an explicit total-cost scenario")
        costs[scenario] = fee
    workspace, replays = prepare_candidate_replays(context, protocol, candidates, candidate_ids)
    tasks = tuple(
        (key, window, scenario, prepared, fee)
        for key in candidate_ids
        for scenario, fee in costs.items()
        for window, prepared in replays[key].items()
    )
    def compute(task):
        key, window, scenario, prepared, fee = task
        result = execute_candidate_replay(context, workspace, prepared, fee)[1]
        return _observation(context, key, window, tier, scenario, result, workspace.replay_data)
    if context.workers == 1 or len(tasks) < 2:
        return tuple(map(compute, tasks))
    with ThreadPoolExecutor(max_workers=min(context.workers, len(tasks))) as executor:
        return tuple(executor.map(compute, tasks))
