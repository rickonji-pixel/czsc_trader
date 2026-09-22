"""Host adapter providing strategy-neutral facts to SRT chart renderers."""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
from strategy_runtime import CHART_CONTEXT_VERSION

from czsc_trader.charting import _normalize_daily

from .execution_data import BacktestExecutionData
from .result import BacktestResult
from .signal_replay import SignalReplay


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def build_backtest_chart_context(
    signal_replay: SignalReplay,
    execution_data: BacktestExecutionData,
    result: BacktestResult,
    initial_cash: float,
) -> dict[str, Any]:
    """Build the immutable host facts consumed by one strategy-owned TDR chart."""

    prices = _normalize_daily(execution_data.adjusted_daily).loc[
        signal_replay.evaluation_start : signal_replay.evaluation_end
    ]
    bars = prices.reset_index(names="date")
    market_columns = [
        column
        for column in ("date", "open", "high", "low", "close", "vol", "amount")
        if column in bars
    ]
    chart_rows = (
        result.decisions
        if signal_replay.chart_data is None
        else signal_replay.chart_data
    )
    snapshot = signal_replay.snapshot
    return {
        "contract_version": CHART_CONTEXT_VERSION,
        "mode": "BACKTEST",
        "strategy": {
            "strategy_id": result.identity.reference.partition("-")[0],
            "reference_id": result.identity.reference,
            "identity_hash": snapshot.source_hash,
            "symbol": execution_data.symbol,
            "configuration": dict(snapshot.strategy_payload),
        },
        "window": {
            "evaluation_start": signal_replay.evaluation_start.date().isoformat(),
            "evaluation_end": signal_replay.evaluation_end.date().isoformat(),
        },
        "market_data": {
            "identity": execution_data.fingerprint,
            "adjustment": "hfq",
            "bars": _records(bars[market_columns]),
        },
        "strategy_output": {
            "decisions": _records(result.decisions),
            "chart_rows": _records(chart_rows),
            "support": dict(signal_replay.support_data or {}),
        },
        "execution": {
            "orders": _records(result.orders),
            "fills": _records(result.fills),
            "account_daily": _records(result.account_daily),
            "trades": _records(result.trades),
            "initial_cash": float(initial_cash),
        },
        "render": {"format": "html", "plotly_runtime": "embedded"},
    }
