from __future__ import annotations

from pathlib import Path
from dataclasses import replace

import pandas as pd
import pytest

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import load_replay_data, resolve_registered_strategy
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.backtesting.signal_replay import replay_signals
from czsc_trader.backtesting.audit_adapter import build_replay_evidence
from czsc_trader.backtesting.service import BacktestRequestV2, run_backtest_v2
from strategy_evaluator import AuditStatus, audit_replay

from functional_support import invoke_main


METRIC_KEYS = {
    "max_drawdown",
    "calmar",
    "win_loss_ratio",
    "win_loss_ratio_status",
    "return",
    "sharpe",
}


def test_backtest_v2_replays_strategy_snapshot_with_empty_account(
    functional_repo: Path,
) -> None:
    context = RepositoryContext.discover(functional_repo)
    snapshot = resolve_registered_strategy(context, "S001", "v1")
    data = load_replay_data(
        context, "backtest", "588080.SH", "etf", pd.Timestamp("2026-09-02").date()
    )
    signals = replay_signals(
        snapshot,
        data,
        pd.Timestamp("2026-01-01").date(),
        pd.Timestamp("2026-09-02").date(),
    )
    result = replay_account(signals, data, 100_000)

    assert result.identity.reference == "S001-v1"
    assert result.account_daily.iloc[0]["cash_before"] == 100_000
    assert result.orders["quantity"].mod(100).eq(0).all()
    assert set(result.fills["trigger"]) <= {"OPEN", "INTRADAY_LIMIT"}
    assert result.decisions["signal_date"].max() <= pd.Timestamp("2026-09-02")
    assert result.account_daily["equity"].gt(0).all()
    evidence = build_replay_evidence(signals, data, result, 100_000)
    assert audit_replay(evidence).status is AuditStatus.PASS
    corrupted = list(evidence.account_daily)
    corrupted[-1] = {**corrupted[-1], "equity": corrupted[-1]["equity"] + 1}
    tampered = audit_replay(replace(evidence, account_daily=tuple(corrupted)))
    assert tampered.status is AuditStatus.FAIL
    assert "ACCOUNT_LEDGER_MISMATCH" in tampered.reason_codes

    summary = run_backtest_v2(
        snapshot=snapshot,
        replay_data=data,
        request=BacktestRequestV2(
            symbol="588080.SH",
            asset_type="etf",
            dataset="backtest",
            start=pd.Timestamp("2026-01-01").date(),
            end=pd.Timestamp("2026-09-02").date(),
            initial_cash=100_000,
        ),
        outputs_root=functional_repo / "outputs",
        run_date=pd.Timestamp("2026-09-04").date(),
    )
    required = {
        "manifest.json", "decisions.csv", "orders.csv", "fills.csv",
        "account_daily.csv", "trades.csv", "metrics.json", "audit.json",
        "report.md", "chart.html",
    }
    assert required == {path.name for path in summary.output_dir.iterdir()}
    assert METRIC_KEYS < set(summary.metrics)
    assert summary.metrics["closed_trades"] == 6


def test_ft_t03_backtest_publishes_audited_metrics_orders_and_reports(
    functional_repo: Path, capsys
) -> None:
    payload = invoke_main(
        [
            "backtest",
            "run",
            "--symbol",
            "588080.SH",
            "--asset",
            "etf",
            "--start",
            "2026-01-01",
            "--end",
            "2026-08-21",
            "--outputs-root",
            str(functional_repo / "outputs"),
            "--repo-root",
            str(functional_repo),
        ],
        capsys,
    )

    output_dir = Path(payload["artifacts"]["output_dir"])
    full = payload["result"]["windows"]["full"]
    strategies = full["strategies"]
    assert set(strategies) == {
        "active_baseline",
        "active_baseline_execution",
        "buyhold",
        "ma5_ma20",
    }
    assert all(set(metrics) == METRIC_KEYS for metrics in strategies.values())
    assert strategies["active_baseline"]["return"] == pytest.approx(
        0.7528525916956634
    )
    assert strategies["ma5_ma20"]["max_drawdown"] == pytest.approx(
        -0.22945460734778733
    )
    assert strategies["ma5_ma20"]["calmar"] == pytest.approx(1.520558369439513)
    assert strategies["ma5_ma20"]["win_loss_ratio"] == pytest.approx(
        3.2767930702460384
    )
    assert strategies["buyhold"]["win_loss_ratio"] is None
    assert strategies["buyhold"]["win_loss_ratio_status"] == "NO_CLOSED_TRADES"

    required = {
        "manifest.json",
        "audit.json",
        "report.md",
        "chart.html",
        "ma_signals.csv",
        "ma_orders.csv",
        "ma_equity.csv",
        "ma_chart.html",
        "execution_orders.csv",
        "execution_equity.csv",
    }
    assert required <= {path.name for path in output_dir.iterdir()}
    execution_orders = pd.read_csv(output_dir / "execution_orders.csv")
    assert (execution_orders["size"] % 100 == 0).all()
    buy_limits = execution_orders.loc[
        execution_orders["side"] == "Buy", "entry_limit"
    ]
    assert (buy_limits * 1000 % 1 < 1e-9).all()

    ma_signals = pd.read_csv(output_dir / "ma_signals.csv", parse_dates=["dt"])
    ma_orders = pd.read_csv(
        output_dir / "ma_orders.csv", parse_dates=["signal_date", "execution_date"]
    )
    sessions = ma_signals["dt"].tolist()
    next_session = dict(zip(sessions, sessions[1:]))
    assert all(
        next_session[order.signal_date] == order.execution_date
        for order in ma_orders.itertuples()
    )
    report = (output_dir / "report.md").read_text(encoding="utf-8")
    assert "| 策略 | 最大回撤 | 卡玛比率 | 盈亏比 | 收益率 | 夏普率 |" in report
    assert "无闭合交易" in report
