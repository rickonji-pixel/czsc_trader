from __future__ import annotations

from typing import Any

import pandas as pd
from strategy_evaluator import ReplayEvidence

from .datasets import ReplayData
from .result import BacktestResult
from .signal_replay import SignalReplay


def _records(frame: pd.DataFrame, date_columns: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
    value = frame.copy()
    for column in date_columns:
        if column in value:
            value[column] = value[column].map(
                lambda item: None if pd.isna(item) else pd.Timestamp(item).isoformat()
            )
    value = value.replace({float("nan"): None})
    return tuple(value.to_dict(orient="records"))


def build_replay_evidence(
    signals: SignalReplay,
    data: ReplayData,
    result: BacktestResult,
    initial_cash: float,
    metrics: dict[str, object],
) -> ReplayEvidence:
    spec = signals.snapshot.resolved_rule.execution
    if spec is None:
        raise ValueError("strategy snapshot has no execution specification")
    start = signals.calculation_start
    end = signals.evaluation_end
    daily = data.execution_daily.loc[data.execution_daily["dt"].between(start, end)].rename(
        columns={"dt": "date"}
    )
    intraday = data.execution_intraday.loc[
        data.execution_intraday["dt"].between(start, end + pd.Timedelta(days=1))
    ].rename(columns={"dt": "time"})
    return ReplayEvidence(
        strategy_hash=signals.snapshot.content_hash,
        data_hash=data.fingerprint,
        initial_cash=float(initial_cash),
        execution_spec={
            "entry_limit_parameter": spec.entry_limit_parameter,
            "exit_limit_ratio": spec.exit_limit_ratio,
            "fee_rate": spec.capital.fee_rate,
            "instrument": {
                "lot_size": spec.instrument.lot_size,
                "price_tick": spec.instrument.price_tick,
                "price_limit_ratio": spec.instrument.price_limit_ratio,
                "maximum_order_quantity": spec.instrument.maximum_order_quantity,
            },
        },
        decisions=_records(result.decisions, ("signal_date", "valid_session")),
        orders=_records(result.orders, ("signal_date", "execution_date")),
        fills=_records(result.fills, ("signal_date", "fill_time")),
        account_daily=_records(result.account_daily, ("date", "signal_date")),
        trades=_records(result.trades, ("entry_date", "exit_date")),
        metrics=metrics,
        execution_daily=_records(daily, ("date",)),
        execution_intraday=_records(intraday, ("time",)),
    )
