"""TDR host adapter for strategy-owned SRT backtest charts."""

from __future__ import annotations

from strategy_runtime import ChartRuntime

from .chart_context import build_backtest_chart_context
from .execution_data import BacktestExecutionData
from .result import BacktestResult
from .signal_replay import SignalReplay


def render_backtest_chart_html(
    signal_replay: SignalReplay,
    execution_data: BacktestExecutionData,
    result: BacktestResult,
    initial_cash: float,
) -> str:
    """Render the audited TDR result exclusively through its SRT chart contract."""

    reference = result.identity.reference
    snapshot = signal_replay.snapshot
    return ChartRuntime().render_backtest(
        reference,
        build_backtest_chart_context(
            signal_replay, execution_data, result, initial_cash
        ),
        descriptor=snapshot.chart_descriptor,
        source_root=snapshot.runtime_root,
        expected_identity_hash=snapshot.source_hash,
    )
