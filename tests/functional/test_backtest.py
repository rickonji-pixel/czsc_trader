from __future__ import annotations

import json
from pathlib import Path
from dataclasses import replace

import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import load_replay_data, resolve_registered_strategy
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.backtesting.signal_replay import replay_signals
from czsc_trader.backtesting.audit_adapter import build_replay_evidence
from czsc_trader.backtesting.metrics import calculate_metrics
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


def _plotly_payload(html: str) -> tuple[list[dict], dict]:
    source = html.rsplit("Plotly.newPlot(", 1)[1].lstrip()
    decoder = json.JSONDecoder()
    _, consumed = decoder.raw_decode(source)
    source = source[consumed:].lstrip().removeprefix(",").lstrip()
    traces, consumed = decoder.raw_decode(source)
    source = source[consumed:].lstrip().removeprefix(",").lstrip()
    layout, _ = decoder.raw_decode(source)
    return traces, layout


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
    assert set(result.decisions["regime"].dropna()) <= {"trend", "range", "warmup"}
    assert result.account_daily["equity"].gt(0).all()
    evidence = build_replay_evidence(
        signals, data, result, 100_000, calculate_metrics(result, 100_000)
    )
    assert audit_replay(evidence).status is AuditStatus.PASS
    corrupted = list(evidence.account_daily)
    corrupted[-1] = {**corrupted[-1], "equity": corrupted[-1]["equity"] + 1}
    tampered = audit_replay(replace(evidence, account_daily=tuple(corrupted)))
    assert tampered.status is AuditStatus.FAIL
    assert "ACCOUNT_LEDGER_MISMATCH" in tampered.reason_codes
    bad_metrics = audit_replay(replace(evidence, metrics={**evidence.metrics, "return": 9}))
    assert "METRIC_MISMATCH" in bad_metrics.reason_codes

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
        "report.md", "chart.html", "buyhold_account_daily.csv",
        "ma_signals.csv", "ma_orders.csv", "ma_account_daily.csv",
        "ma_trades.csv", "ma_chart.html",
    }
    assert required == {path.name for path in summary.output_dir.iterdir()}
    assert set(summary.metrics) == {"strategy", "benchmarks"}
    assert summary.metrics["strategy"]["reference"] == "S001-v1"
    assert METRIC_KEYS < set(summary.metrics["strategy"]["metrics"])
    assert summary.metrics["strategy"]["metrics"]["closed_trades"] == 7
    assert set(summary.metrics["benchmarks"]) == {"buyhold", "ma5_ma20"}
    for benchmark in summary.metrics["benchmarks"].values():
        assert METRIC_KEYS <= set(benchmark["metrics"])
    ma_orders = pd.read_csv(summary.output_dir / "ma_orders.csv")
    ma_sessions = pd.to_datetime(
        pd.read_csv(summary.output_dir / "ma_signals.csv")["date"]
    )
    next_session = dict(zip(ma_sessions[:-1], ma_sessions[1:], strict=True))
    assert all(
        next_session[pd.Timestamp(row.signal_date)] == pd.Timestamp(row.execution_date)
        for row in ma_orders.itertuples()
    )
    assert pd.read_csv(summary.output_dir / "buyhold_account_daily.csv").iloc[0][
        "equity"
    ] > 0
    report = (summary.output_dir / "report.md").read_text(encoding="utf-8")
    assert "- 计算窗口：2020-11-16—2026-09-02" in report
    assert "- 回测窗口：2026-01-05—2026-09-02，共162个交易日" in report
    assert "| S001-v1 |" in report
    assert "| BuyHold |" in report
    assert "| MA5/MA20 |" in report
    assert "[MA5/MA20图表](ma_chart.html)" in report
    ma_chart = (summary.output_dir / "ma_chart.html").read_text(encoding="utf-8")
    ma_traces, _ = _plotly_payload(ma_chart)
    assert {trace["name"] for trace in ma_traces} >= {"日K", "MA5", "MA20"}
    chart = (summary.output_dir / "chart.html").read_text(encoding="utf-8")
    traces, layout = _plotly_payload(chart)
    by_name = {trace["name"]: trace for trace in traces}
    assert layout["title"]["text"] == "S001-v1 确定性回测｜2026-01-05—2026-09-02"
    assert layout["hovermode"] == "x unified"
    for axis_name in ("xaxis", "xaxis2"):
        assert layout[axis_name]["showspikes"] is True
        assert layout[axis_name]["spikecolor"] == "#64748b"
        assert layout[axis_name]["spikedash"] == "dot"
        assert layout[axis_name]["spikemode"] == "across"
        assert layout[axis_name]["spikesnap"] == "data"
        assert layout[axis_name]["spikethickness"] == 1
    assert by_name["CZSC笔"]["line"]["width"] == 1
    assert by_name["CZSC笔"]["marker"]["size"] == 3
    for name, symbol, color, angle in (
        ("买入信号", "triangle-down-open", "#ef4444", -180),
        ("卖出信号", "triangle-down-open", "#22c55e", 0),
        ("买入成交", "triangle-down", "#ef4444", -180),
        ("卖出成交", "triangle-down", "#22c55e", 0),
    ):
        assert by_name[name]["marker"] == {
            "color": color,
            "size": 10,
            "symbol": symbol,
            "line": {"width": 1},
            "angle": angle,
            "angleref": "up",
        }
        assert by_name[name]["hoverinfo"] == "skip"
    marker_y = {
        name: {
            str(pd.Timestamp(day).date()): y
            for day, y in zip(by_name[name]["x"], by_name[name]["y"], strict=True)
        }
        for name in ("买入信号", "卖出信号", "买入成交", "卖出成交")
    }
    assert abs(layout["yaxis"]["range"][0] - 1.18175288) < 1e-12
    assert abs(layout["yaxis"]["range"][1] - 2.44912832) < 1e-12
    assert abs(marker_y["买入成交"]["2026-01-06"] - 1.352674896) < 1e-12
    assert abs(marker_y["卖出信号"]["2026-01-29"] - 1.774971824) < 1e-12
    assert abs(marker_y["卖出成交"]["2026-01-30"] - 1.774971824) < 1e-12
    assert marker_y["买入信号"]["2026-04-01"] < 1.2934257
    assert marker_y["买入成交"]["2026-04-02"] == marker_y["买入信号"]["2026-04-01"]
    assert marker_y["卖出信号"]["2026-07-13"] > 2.2905044500000002
    assert marker_y["卖出成交"]["2026-07-14"] == marker_y["卖出信号"]["2026-07-13"]
    assert marker_y["卖出信号"]["2026-08-14"] > 1.847751
    assert marker_y["卖出成交"]["2026-08-17"] == marker_y["卖出信号"]["2026-08-14"]
    assert marker_y["买入信号"]["2026-08-27"] < 1.6902378
    assert marker_y["买入成交"]["2026-08-28"] == marker_y["买入信号"]["2026-08-27"]
    assert "目标持仓" not in by_name
    assert "实际持仓" not in by_name
    assert by_name["策略得分"]["yaxis"] == "y2"
    assert by_name["策略得分"]["line"] == {"color": "#fbbf24", "width": 2}
    assert by_name["买入阈值"]["yaxis"] == "y2"
    assert by_name["买入阈值"]["line"] == {
        "color": "#ef4444", "dash": "dash", "width": 1
    }
    assert by_name["卖出阈值"]["yaxis"] == "y2"
    assert by_name["卖出阈值"]["line"] == {
        "color": "#22c55e", "dash": "dash", "width": 1
    }
    details = {
        str(pd.Timestamp(day).date()): text
        for day, text in zip(
            by_name["交易日详情"]["x"], by_name["交易日详情"]["text"], strict=True
        )
    }
    assert "买入成交" in details["2026-01-06"]
    assert "成交" not in details["2026-01-07"]
    assert "成交" not in details["2026-01-08"]
    assert "卖出信号" in details["2026-01-29"]
    assert "成交" not in details["2026-01-29"]
    assert "策略得分" in details["2026-01-29"]
    assert "行情状态" in details["2026-01-29"]
    assert "买入阈值 0.175" in details["2026-01-29"]
    assert "卖出阈值 0.025" in details["2026-01-29"]


def test_ft_t03_backtest_publishes_audited_metrics_orders_and_reports(
    functional_repo: Path, capsys
) -> None:
    payload = invoke_main(
        [
            "backtest",
            "run",
            "--strategy",
            "S001",
            "--strategy-version",
            "v1",
            "--dataset",
            "backtest",
            "--symbol",
            "588080.SH",
            "--asset",
            "etf",
            "--start",
            "2026-01-01",
            "--end",
            "2026-09-02",
            "--init-cash",
            "100000",
            "--outputs-root",
            str(functional_repo / "outputs"),
            "--repo-root",
            str(functional_repo),
        ],
        capsys,
    )

    output_dir = Path(payload["artifacts"]["output_dir"])
    assert payload["result"]["strategy"] == "S001-v1"
    assert payload["result"]["audit_status"] == "PASS"
    assert METRIC_KEYS < set(payload["result"]["metrics"]["strategy"]["metrics"])
    assert set(payload["result"]["metrics"]["benchmarks"]) == {
        "buyhold", "ma5_ma20"
    }

    required = {
        "manifest.json",
        "audit.json",
        "report.md",
        "chart.html",
        "decisions.csv",
        "orders.csv",
        "fills.csv",
        "account_daily.csv",
        "trades.csv",
        "metrics.json",
        "buyhold_account_daily.csv",
        "ma_signals.csv",
        "ma_orders.csv",
        "ma_account_daily.csv",
        "ma_trades.csv",
        "ma_chart.html",
    }
    assert required <= {path.name for path in output_dir.iterdir()}
    execution_orders = pd.read_csv(output_dir / "orders.csv")
    assert (execution_orders["quantity"] % 100 == 0).all()
    buy_limits = execution_orders.loc[
        execution_orders["side"] == "BUY", "limit_price"
    ]
    assert (buy_limits * 1000 % 1 < 1e-9).all()
    report = (output_dir / "report.md").read_text(encoding="utf-8")
    assert "| 策略 | 最大回撤 | 卡玛比率 | 盈亏比 | 收益率 | 夏普率 |" in report
