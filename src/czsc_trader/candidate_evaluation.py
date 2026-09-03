"""Run complete candidate payloads through Trader's existing backtest semantics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from strategy_evaluator import EvaluationProtocol, MetricObservation, MetricStatus

from .backtest import run_period_backtests
from .backtest_runner import _complete_baseline_execution_results
from .baseline_execution import apply_resolved_baseline
from .baselines import resolve_strategy_payload
from .data import load_market_data
from .factors import generate_factor_frame
from .strategy_metrics import closed_trade_ledger


@dataclass(frozen=True)
class CandidateEvaluationContext:
    repository: Any
    symbol: str
    asset_type: str
    periods: tuple[tuple[str, tuple[pd.Timestamp, pd.Timestamp]], ...]
    fee_rate: float = 0.0005
    init_cash: float = 1_000_000.0


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


def _observation(candidate_id: str, window: str, tier: str, scenario: str, equity: pd.Series, orders: pd.DataFrame, init_cash: float) -> MetricObservation:
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
        (("net_cagr", net_cagr), ("total_return", total_return), (f"{window}_return", total_return)),
    )


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
    data = load_market_data(context.repository.raw_dir, context.symbol, context.asset_type).truncate(protocol.development_cutoff)
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
        )
        applied = apply_resolved_baseline(factors.frame, baseline, daily_close=daily_close)
        for scenario in scenarios:
            if scenario != "standard" and tier != "STRESS":
                raise ValueError("non-standard scenarios require STRESS tier")
            if scenario == "standard":
                fee_rate = context.fee_rate
            elif scenario.startswith("fee_x"):
                fee_rate = context.fee_rate * float(scenario.removeprefix("fee_x"))
            else:
                raise ValueError(f"unsupported stress scenario: {scenario}")
            results = run_period_backtests(data.daily, applied.target_position, periods, fee_rate=fee_rate, init_cash=context.init_cash)
            execution_results = {}
            if tier in {"FORMAL", "STRESS"} and baseline.execution is not None:
                execution_results = _complete_baseline_execution_results(
                    data.daily, data.intraday, applied.target_position, periods, baseline.execution,
                    fee_rate=fee_rate, init_cash=context.init_cash,
                )
            for window, result in results.items():
                effective = execution_results.get(window, result)
                output.append(_observation(candidate_id, window, tier, scenario, effective.equity, effective.orders, context.init_cash))
    return tuple(output)
