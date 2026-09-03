from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from functional_support import invoke_main


METRIC_KEYS = {
    "max_drawdown",
    "calmar",
    "win_loss_ratio",
    "win_loss_ratio_status",
    "return",
    "sharpe",
}


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
