from __future__ import annotations

from dataclasses import replace
from datetime import date
import json
from pathlib import Path

import pandas as pd
import pytest

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import resolve_registered_strategy
from czsc_trader.backtesting.execution_data import prepare_backtest_execution_data
from czsc_trader.backtesting.chart import render_backtest_chart_html
from czsc_trader.backtesting.audit_adapter import build_replay_evidence
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.backtesting.service import BacktestRequestV2, run_backtest_v2
from czsc_trader.backtesting.srt_bridge import (
    build_srt_signal_replay,
    replay_srt_account,
    srt_data_directory,
)
from strategy_runtime import RuntimeContractError
from strategy_evaluator import AuditStatus, audit_replay

from functional_support import invoke_main, invoke_main_failure


METRIC_KEYS = {
    "max_drawdown",
    "calmar",
    "win_loss_ratio",
    "win_loss_ratio_status",
    "return",
    "sharpe",
}


def test_tdr_allocates_one_human_readable_reusable_srt_space(
    functional_repo: Path,
) -> None:
    context = RepositoryContext.discover(functional_repo)
    snapshot = resolve_registered_strategy(context, "S001", "v1")

    created = srt_data_directory(
        context.tdr_srt_root,
        snapshot,
        "588080.SH",
        created_on=date(2026, 9, 22),
    )
    reused = srt_data_directory(
        context.tdr_srt_root,
        snapshot,
        "588080.SH",
        created_on=date(2026, 9, 23),
    )

    assert created == context.tdr_srt_root / "S001v1_588080_260922"
    assert reused == created

    (context.tdr_srt_root / "S001v1_588080_260921").mkdir()
    with pytest.raises(RuntimeContractError, match="multiple reusable"):
        srt_data_directory(context.tdr_srt_root, snapshot, "588080.SH")


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
    execution_data = prepare_backtest_execution_data(
        srt_data_root=context.tdr_srt_root,
        symbol="588080.SH",
        asset_type="etf",
        start=pd.Timestamp("2026-01-01").date(),
        end=pd.Timestamp("2026-09-02").date(),
        env_file=context.root / ".env",
    )
    strategy, signals = build_srt_signal_replay(
        snapshot=snapshot,
        execution_data=execution_data,
        start=pd.Timestamp("2026-01-01"),
        end=pd.Timestamp("2026-09-02"),
        repository_root=functional_repo,
    )
    result = replay_srt_account(
        strategy=strategy,
        signals=signals,
        execution_data=execution_data,
        initial_cash=100_000,
    )

    assert result.identity.reference == "S001-v1"
    assert result.account_daily.iloc[0]["cash_before"] == 100_000
    assert result.orders["quantity"].mod(100).eq(0).all()
    assert set(result.orders.loc[result.orders["side"].eq("BUY"), "order_type"]) == {
        "LIMIT"
    }
    assert set(result.orders.loc[result.orders["side"].eq("SELL"), "order_type"]) == {
        "MARKET"
    }
    assert set(result.fills["trigger"]) <= {
        "OPEN",
        "OPEN_MARKET",
        "INTRADAY_LIMIT",
    }
    assert result.decisions["signal_date"].max() <= pd.Timestamp("2026-09-02")
    assert set(result.decisions["regime"].dropna()) <= {"trend", "range", "warmup"}
    assert result.account_daily["equity"].gt(0).all()
    chart = render_backtest_chart_html(signals, execution_data, result, 100_000)
    traces, _ = _plotly_payload(chart)
    trace_names = {trace["name"] for trace in traces}
    assert {"策略得分", "买入阈值", "卖出阈值"} <= trace_names
    assert "基础分" not in trace_names
    assert "确认分" not in trace_names
    assert "TDR · DETERMINISTIC BACKTEST" in chart
    assert "class=\"metrics\"" in chart
    by_name = {trace["name"]: trace for trace in traces}
    hover_text = "\n".join(by_name["交易日详情"]["text"])
    assert "策略得分" in hover_text
    assert "行情状态" in hover_text
    evidence = build_replay_evidence(
        signals, execution_data, result, 100_000, calculate_metrics(result, 100_000)
    )
    assert audit_replay(evidence).status is AuditStatus.PASS
    corrupted = list(evidence.account_daily)
    corrupted[-1] = {**corrupted[-1], "equity": corrupted[-1]["equity"] + 1}
    tampered = audit_replay(replace(evidence, account_daily=tuple(corrupted)))
    assert tampered.status is AuditStatus.FAIL
    assert "ACCOUNT_LEDGER_MISMATCH" in tampered.reason_codes
    omitted = audit_replay(
        replace(evidence, account_daily=evidence.account_daily[:2] + evidence.account_daily[3:])
    )
    assert omitted.status is AuditStatus.FAIL
    assert "INCOMPLETE_ACCOUNT_SESSION_COVERAGE" in omitted.reason_codes
    bad_metrics = audit_replay(replace(evidence, metrics={**evidence.metrics, "return": 9}))
    assert "METRIC_MISMATCH" in bad_metrics.reason_codes

    summary = run_backtest_v2(
        snapshot=snapshot,
        request=BacktestRequestV2(
            symbol="588080.SH",
            asset_type="etf",
            start=pd.Timestamp("2026-01-01").date(),
            end=pd.Timestamp("2026-09-02").date(),
            initial_cash=100_000,
        ),
        srt_data_root=functional_repo / "data" / "backtest",
        outputs_root=functional_repo / "outputs",
        run_date=pd.Timestamp("2026-09-04").date(),
        repository_root=functional_repo,
        execution_data=execution_data,
    )
    required = {
        "manifest.json", "decisions.csv", "orders.csv", "fills.csv",
        "account_daily.csv", "trades.csv", "metrics.json", "audit.json",
        "report.md", "chart.html", "buyhold_account_daily.csv",
        "ma_signals.csv", "ma_orders.csv", "ma_account_daily.csv",
        "ma_trades.csv", "ma_chart.html",
    }
    assert required == {path.name for path in summary.output_dir.iterdir()}
    assert summary.output_dir.name == "S001v1_588080_0904_BT01"
    assert not any((functional_repo / ".tmp" / "backtest").iterdir())
    assert set(summary.metrics) == {"strategy", "benchmarks"}
    assert summary.metrics["strategy"]["reference"] == "S001-v1"
    assert summary.metrics["strategy"]["metrics"] == calculate_metrics(result, 100_000)
    assert METRIC_KEYS < set(summary.metrics["strategy"]["metrics"])
    assert signals.decisions.iloc[0]["target_position"] == 1.0
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
    assert "- 策略研发窗口：2020-01-01—2026-09-02" in report
    calculation_line = next(line for line in report.splitlines() if line.startswith("- 计算窗口："))
    assert calculation_line.endswith("—2026-09-01")
    assert "- 回测窗口：2026-01-05—2026-09-02，共162个交易日" in report
    assert "| 策略 | 收益率 | 最大回撤 | 卡玛比率 | 盈亏比 | 夏普率 | 闭合交易 |" in report
    assert "| S001-v1 |" in report and "| 7 |" in report
    assert "| BuyHold |" in report
    assert "| MA5/MA20 |" in report
    assert "[MA5/MA20图表](ma_chart.html)" in report
    ma_chart = (summary.output_dir / "ma_chart.html").read_text(encoding="utf-8")
    ma_traces, ma_layout = _plotly_payload(ma_chart)
    assert {trace["name"] for trace in ma_traces} >= {"日K", "MA5", "MA20"}
    assert ma_layout["hoverlabel"]["bgcolor"] == "rgba(255, 255, 255, 0.5)"
    chart = (summary.output_dir / "chart.html").read_text(encoding="utf-8")
    traces, layout = _plotly_payload(chart)
    by_name = {trace["name"]: trace for trace in traces}
    assert "588080.SH <span>|</span> S001-v1 <span>|</span> " in chart
    assert "2026.01.05 - 2026.09.02" in chart
    assert layout["hovermode"] == "x unified"
    assert layout["hoverlabel"]["bgcolor"] == "rgba(5, 13, 24, 0.5)"
    assert ".metric-value.positive{color:var(--red)}" in chart
    assert ".metric-value.negative{color:var(--green)}" in chart
    assert by_name["日K"]["increasing"]["line"]["color"] == "#ef4444"
    assert by_name["日K"]["decreasing"]["line"]["color"] == "#22c55e"
    for axis_name in ("xaxis", "xaxis2"):
        assert layout[axis_name]["showspikes"] is False
    shared_guide = layout["shapes"][0]
    assert shared_guide["xref"] == "x"
    assert shared_guide["yref"] == "paper"
    assert shared_guide["y0"] == 0
    assert shared_guide["y1"] == 1
    assert shared_guide["visible"] is False
    assert shared_guide["line"] == {
        "color": "#8195ad", "dash": "dot", "width": 1
    }
    assert "chart.on('plotly_hover'" in chart
    assert "'shapes[0].visible': true" in chart
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
    for name in ("策略得分", "买入阈值", "卖出阈值"):
        assert by_name[name]["xaxis"] == "x2"
    assert by_name["策略得分"]["line"] == {"color": "#4fa5ff", "width": 2}
    assert by_name["买入阈值"]["yaxis"] == "y2"
    assert by_name["买入阈值"]["line"] == {
        "color": "#ef4444", "dash": "dash", "width": 1
    }
    assert by_name["卖出阈值"]["yaxis"] == "y2"
    assert by_name["卖出阈值"]["line"] == {
        "color": "#22c55e", "dash": "dash", "width": 1
    }
    assert layout["yaxis"]["title"]["text"] == "后复权价格"
    assert layout["yaxis2"]["title"]["text"] == "策略得分"
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


def test_backtest_does_not_read_legacy_srt_publication_manifests(
    functional_repo: Path, capsys, monkeypatch
) -> None:
    monkeypatch.chdir(functional_repo)
    data_root = functional_repo / "data" / "backtest"
    for name in ("588080_manifest.json", "588080_strategy_generation.json"):
        (data_root / name).write_text("{}\n", encoding="utf-8")

    payload = invoke_main(
        [
            "backtest", "run",
            "--strategy", "S001",
            "--strategy-version", "v1",
            "--symbol", "588080.SH",
            "--asset", "etf",
            "--start", "2026-01-05",
            "--end", "2026-01-30",
            "--init-cash", "100000",
        ],
        capsys,
    )

    assert payload["result"]["runtime_engine"] == "srt"
    assert payload["result"]["audit_status"] == "PASS"


def test_ft_t03_backtest_publishes_audited_metrics_orders_and_reports(
    functional_repo: Path, capsys, monkeypatch
) -> None:
    monkeypatch.chdir(functional_repo)
    payload = invoke_main(
        [
            "backtest",
            "run",
            "--strategy",
            "S001",
            "--strategy-version",
            "v1",
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
    assert "| 策略 | 收益率 | 最大回撤 | 卡玛比率 | 盈亏比 | 夏普率 | 闭合交易 |" in report
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["signal_support"]["mode"] == "srt_input_contract"
    assert len(manifest["signal_support"]["runtime_sha256"]) == 64
    assert manifest["signal_support"]["execution_policy"]["policy_type"] == "FROZEN_RULE"

    generalized = invoke_main(
        [
            "backtest", "run",
            "--strategy", "S001", "--strategy-version", "v1",
            "--symbol", "159352.SZ", "--asset", "etf",
            "--start", "2026-01-01", "--end", "2026-09-02", "--init-cash", "100000",
        ],
        capsys,
    )
    generalized_dir = Path(generalized["artifacts"]["output_dir"])
    generalized_manifest = json.loads(
        (generalized_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert generalized["result"]["audit_status"] == "PASS"
    assert generalized_manifest["application"] == {
        "mode": "cross_symbol_generalization",
        "strategy_reference_symbol": "588080.SH",
        "backtest_symbol": "159352.SZ",
        "runtime_engine": "srt",
    }
    generalized_report = (generalized_dir / "report.md").read_text(encoding="utf-8")
    assert "- 策略参考标的：588080.SH" in generalized_report
    assert "- 实际回测标的：159352.SZ" in generalized_report
    assert "- 应用方式：跨标的泛化测试" in generalized_report


def test_backtest_rejects_false_success_beyond_published_session(
    functional_repo: Path, capsys, monkeypatch
) -> None:
    monkeypatch.chdir(functional_repo)
    payload = invoke_main_failure(
        [
            "backtest", "run",
            "--strategy", "S001", "--strategy-version", "v1",
            "--symbol", "588080.SH", "--asset", "etf",
            "--start", "2026-01-01", "--end", "2026-09-06", "--init-cash", "100000",
        ],
        capsys,
    )

    assert payload["error"] == {
        "code": "backtest_data_not_ready",
        "message": (
            "backtest data is not ready: requested cutoff 2026-09-06 includes "
            "unpublished trading session 2026-09-03; published cutoff is 2026-09-02"
        ),
        "context": {
            "strategy": "S001-v1",
            "symbol": "588080.SH",
            "requested_cutoff": "2026-09-06",
            "published_cutoff": "2026-09-02",
            "first_unpublished_session": "2026-09-03",
        },
    }
    assert list((functional_repo / "outputs").iterdir()) == []


def test_backtest_historical_window_remains_valid_after_source_advances(
    functional_repo: Path, capsys, monkeypatch
) -> None:
    monkeypatch.chdir(functional_repo)
    payload = invoke_main(
        [
            "backtest", "run",
            "--strategy", "S001", "--strategy-version", "v1",
            "--symbol", "588080.SH", "--asset", "etf",
            "--start", "2026-06-25", "--end", "2026-08-31", "--init-cash", "100000",
        ],
        capsys,
    )

    assert payload["status"] == "PASS"
    assert payload["result"]["audit_status"] == "PASS"
