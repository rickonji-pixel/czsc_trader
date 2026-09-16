from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil
from uuid import uuid4

import pandas as pd
import pytest

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import load_replay_data, resolve_registered_strategy
from czsc_trader.backtesting.closing_dislocation_replay import (
    build_closing_dislocation_signals,
)
from czsc_trader.backtesting.chart import render_backtest_chart_html
from czsc_trader.backtesting.causal_feature_gate_replay import (
    build_causal_feature_gate_signals,
)
from czsc_trader.backtesting.strategy_source import resolve_candidate_snapshot
from czsc_trader.identity import canonical_json_sha256
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
    chart = render_backtest_chart_html(signals, data, result)
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
    assert not any((functional_repo / ".tmp" / "backtest").iterdir())
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
    assert "- 策略研发窗口：2020-01-01—2026-09-02" in report
    assert "- 计算窗口：2020-11-16—2026-09-02" in report
    assert "- 回测窗口：2026-01-05—2026-09-02，共162个交易日" in report
    assert "| 策略 | 收益率 | 最大回撤 | 卡玛比率 | 盈亏比 | 夏普率 |" in report
    assert "| S001-v1 |" in report
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


def test_ft_t03_s002_event_hold_replays_with_formal_execution() -> None:
    repo = Path(__file__).resolve().parents[2]
    context = RepositoryContext.discover(repo)
    candidate = json.loads(
        (repo / "experiments/S002/20260910_S002_EX12/candidate_payload.json").read_text(
            encoding="utf-8"
        )
    )
    proposed = json.loads(
        (
            repo
            / "experiments/S002/20260910_S002_EX14/artifacts/proposed_strategy_payload.json"
        ).read_text(encoding="utf-8")
    )
    candidate_hash = canonical_json_sha256(candidate)
    original = resolve_candidate_snapshot(
        context, "S002-C001", candidate, candidate_hash, "candidate_payload.json"
    )
    deployed = resolve_candidate_snapshot(
        context,
        "S002-C001-EXECUTION",
        proposed,
        canonical_json_sha256(proposed),
        "proposed_strategy_payload.json",
    )
    data = load_replay_data(
        context, "research", "510500.SH", "etf", pd.Timestamp("2026-09-08").date()
    )
    original_signals = replay_signals(
        original, data, pd.Timestamp("2021-01-01").date(), pd.Timestamp("2026-09-08").date()
    )
    deployed_signals = replay_signals(
        deployed, data, pd.Timestamp("2021-01-01").date(), pd.Timestamp("2026-09-08").date()
    )
    assert original_signals.decisions["target_position"].equals(
        deployed_signals.decisions["target_position"]
    )
    assert deployed_signals.decisions["target_position"].diff().eq(1).sum() == 26
    result = replay_account(deployed_signals, data, 100_000)
    metrics = calculate_metrics(result, 100_000)
    assert metrics["closed_trades"] == 25
    assert metrics["return"] == pytest.approx(0.2514063685)
    evidence = build_replay_evidence(deployed_signals, data, result, 100_000, metrics)
    assert audit_replay(evidence).status is AuditStatus.PASS


def test_ft_t03_s007_causal_feature_gate_replays_frozen_candidate() -> None:
    repo = Path(__file__).resolve().parents[2]
    context = RepositoryContext.discover(repo)
    payload_path = repo / "experiments/S007/20260915_S007_EX31/candidate_payload.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    candidate_hash = canonical_json_sha256(payload)
    snapshot = resolve_candidate_snapshot(
        context,
        "S007-C001",
        payload,
        candidate_hash,
        str(payload_path.relative_to(repo)),
    )
    data = load_replay_data(
        context, "research", "588080.SH", "etf", pd.Timestamp("2026-09-02").date()
    )
    signals = build_causal_feature_gate_signals(
        snapshot,
        data,
        pd.Timestamp("2021-01-05"),
        pd.Timestamp("2026-09-02"),
        repo,
    )
    result = replay_account(signals, data, 100_000)
    metrics = calculate_metrics(result, 100_000)

    assert metrics["closed_trades"] == 139
    assert metrics["return"] == pytest.approx(2.195428348)
    assert len(result.orders) == len(result.fills) == 278
    evidence = build_replay_evidence(signals, data, result, 100_000, metrics)
    assert audit_replay(evidence).status is AuditStatus.PASS

    altered = json.loads(json.dumps(payload))
    altered["rule"]["data_source"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="source is missing or differs"):
        resolve_candidate_snapshot(
            context,
            "S007-C001-TAMPERED",
            altered,
            canonical_json_sha256(altered),
            "tampered",
        )


def test_ft_t03_s007_registered_backtest_uses_independent_support_data() -> None:
    repo = Path(__file__).resolve().parents[2]
    context = RepositoryContext.discover(repo)
    snapshot = resolve_registered_strategy(context, "S007", "v1")
    data = load_replay_data(
        context, "backtest", "588080.SH", "etf", pd.Timestamp("2026-09-15").date()
    )
    with pytest.raises(ValueError, match="strategy support data is missing"):
        build_causal_feature_gate_signals(
            snapshot,
            replace(data, root=repo / ".tmp" / "missing-support"),
            pd.Timestamp("2026-06-25"),
            pd.Timestamp("2026-09-15"),
            repo,
        )

    signals = build_causal_feature_gate_signals(
        snapshot,
        data,
        pd.Timestamp("2026-06-25"),
        pd.Timestamp("2026-09-15"),
        repo,
    )

    assert signals.evaluation_start == pd.Timestamp("2026-06-25")
    assert signals.evaluation_end == pd.Timestamp("2026-09-15")
    assert signals.calculation_end == pd.Timestamp("2026-09-15")
    assert signals.support_data is not None
    assert signals.support_data["mode"] == "backtest_strategy_support"
    assert signals.support_data["last_session"] == "2026-09-15"
    result = replay_account(signals, data, 100_000)
    traces, _ = _plotly_payload(render_backtest_chart_html(signals, data, result))
    by_name = {trace["name"]: trace for trace in traces}
    assert {
        "基础分",
        "确认分",
        "基础分入场阈值",
        "基础分退出阈值",
        "确认门",
    } <= set(by_name)
    hover_text = "\n".join(by_name["交易日详情"]["text"])
    assert "基础得分" in hover_text
    assert "确认得分" in hover_text
    assert "确认分入场门槛" in hover_text
    assert "行情状态 不适用" not in hover_text


def test_ft_t03_s003_intraday_overlay_replays_frozen_candidate_contract() -> None:
    repo = Path(__file__).resolve().parents[2]
    context = RepositoryContext.discover(repo)
    payload = {
        "schema_version": 1,
        "strategy_kind": "constituent_moneyflow_intraday_overlay",
        "symbol": "510500.SH",
        "rule": {
            "symbol": "510500.SH",
            "data_source": {
                "path": (
                    "experiments/S003/20260911_S003_EX43/artifacts/"
                    "constituent_moneyflow_panel.csv.gz"
                ),
                "sha256": "c67f3a21e4180751e65733173d2e2072fccc4011ded14c1fa36d0611fa467062",
            },
            "feature": {
                "minimum_observed_weight_ratio": 0.95,
                "threshold_lookback_sessions": 60,
                "threshold_quantile": 0.80,
                "threshold_excludes_current_session": True,
                "comparison": "GREATER_THAN_OR_EQUAL",
            },
            "execution": {
                "core_fraction": 0.50,
                "event_fraction": 0.50,
                "entry_checkpoint": "OPEN",
                "exit_checkpoint": "11:30_CLOSE",
                "one_way_cost": 0.00012,
                "lot_size": 100,
                "maximum_events_per_day": 1,
                "t_plus_one_inventory_rotation": True,
            },
        },
    }
    snapshot = resolve_candidate_snapshot(
        context,
        "S003-C001-EXECUTION",
        payload,
        canonical_json_sha256(payload),
        "formal-execution-test",
    )
    data = load_replay_data(
        context,
        "research",
        "510500.SH",
        "etf",
        pd.Timestamp("2026-09-08").date(),
        include_five_minute=True,
    )
    outputs_root = repo / ".tmp" / f"s003-e2e-{uuid4().hex}"
    summary = run_backtest_v2(
        snapshot=snapshot,
        replay_data=data,
        request=BacktestRequestV2(
            symbol="510500.SH",
            asset_type="etf",
            dataset="research",
            start=pd.Timestamp("2021-04-15").date(),
            end=pd.Timestamp("2026-09-08").date(),
            initial_cash=100_000,
        ),
        outputs_root=outputs_root,
        run_date=pd.Timestamp("2026-09-11").date(),
        repository_root=repo,
    )
    decisions = pd.read_csv(summary.output_dir / "decisions.csv")
    orders = pd.read_csv(summary.output_dir / "orders.csv")
    account = pd.read_csv(summary.output_dir / "account_daily.csv")
    audit = json.loads((summary.output_dir / "audit.json").read_text(encoding="utf-8"))
    metrics = summary.metrics["strategy"]["metrics"]
    assert len(decisions) == 276
    assert metrics["closed_trades"] == 276
    assert len(orders) == 552
    assert orders["quantity"].mod(100).eq(0).all()
    assert account["quantity"].eq(account["quantity"].iloc[0]).all()
    assert orders.groupby("execution_date")["checkpoint"].agg(list).apply(
        lambda value: value == ["OPEN", "11:30_CLOSE"]
    ).all()
    assert audit["status"] == "PASS"
    assert set(audit["checks"]) == {
        "INTRADAY_ORDER_CONTRACT",
        "INTRADAY_CHECKPOINT_PRICES",
        "T_PLUS_ONE_CORE_ROTATION_LEDGER",
        "INTRADAY_TRADE_PAIRING",
        "INTRADAY_METRICS",
    }
    shutil.rmtree(outputs_root)


def test_ft_t03_s004_closing_dislocation_replays_research_candidate_contract() -> None:
    repo = Path(__file__).resolve().parents[2]
    context = RepositoryContext.discover(repo)
    payload_path = repo / "experiments/S004/20260912_S004_EX13/candidate_payload.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    snapshot = resolve_candidate_snapshot(
        context,
        "S004-C001",
        payload,
        canonical_json_sha256(payload),
        str(payload_path.relative_to(repo)),
    )
    data = load_replay_data(
        context,
        "research",
        "588080.SH",
        "etf",
        pd.Timestamp("2026-09-02").date(),
        include_one_minute=True,
    )
    signals = build_closing_dislocation_signals(
        snapshot,
        data,
        pd.Timestamp("2021-06-02"),
        pd.Timestamp("2026-09-02"),
    )
    result = replay_account(signals, data, 100_000)
    metrics = calculate_metrics(result, 100_000)
    expected = pd.read_csv(
        repo / "experiments/S004/20260912_S004_EX12/artifacts/candidate_episodes.csv.gz",
        parse_dates=["event_date"],
    )

    event_dates = result.orders.loc[
        result.orders["side"].eq("BUY") & result.orders["status"].eq("FILLED"),
        "signal_date",
    ].reset_index(drop=True)
    pd.testing.assert_series_equal(
        event_dates.dt.normalize(),
        expected["event_date"].dt.normalize(),
        check_names=False,
    )
    assert len(result.trades.loc[result.trades["status"].eq("CLOSED")]) == 262
    assert signals.decisions["target_position"].eq(1).sum() == 263
    assert metrics["return"] == pytest.approx(0.8865996828)
    evidence = build_replay_evidence(signals, data, result, 100_000, metrics)
    assert audit_replay(evidence).status is AuditStatus.PASS


def test_ft_t03_s004_margin_filtered_candidate_replays_research_contract() -> None:
    repo = Path(__file__).resolve().parents[2]
    context = RepositoryContext.discover(repo)
    payload_path = repo / "experiments/S004/20260913_S004_EX42/candidate_payload.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    snapshot = resolve_candidate_snapshot(
        context,
        "S004-C002",
        payload,
        canonical_json_sha256(payload),
        str(payload_path.relative_to(repo)),
    )
    data = load_replay_data(
        context,
        "research",
        "588080.SH",
        "etf",
        pd.Timestamp("2026-09-02").date(),
        include_one_minute=True,
    )
    signals = build_closing_dislocation_signals(
        snapshot,
        data,
        pd.Timestamp("2021-06-01"),
        pd.Timestamp("2026-09-02"),
        repo,
    )
    result = replay_account(signals, data, 100_000)
    metrics = calculate_metrics(result, 100_000)
    expected = pd.read_csv(
        repo / "experiments/S004/20260913_S004_EX41/artifacts/candidate_episodes.csv.gz",
        parse_dates=["event_date"],
    )

    event_dates = result.orders.loc[
        result.orders["side"].eq("BUY") & result.orders["status"].eq("FILLED"),
        "signal_date",
    ].reset_index(drop=True)
    pd.testing.assert_series_equal(
        event_dates.dt.normalize(),
        expected["event_date"].dt.normalize(),
        check_names=False,
    )
    assert len(result.trades.loc[result.trades["status"].eq("CLOSED")]) == 160
    assert signals.decisions["target_position"].eq(1).sum() == 161
    assert signals.decisions["entry_risk_denied"].sum() == 102
    assert metrics["return"] == pytest.approx(1.5210945141)
    evidence = build_replay_evidence(signals, data, result, 100_000, metrics)
    assert audit_replay(evidence).status is AuditStatus.PASS


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

    source_root = functional_repo / "data" / "backtest"
    for source in source_root.glob("588080*"):
        destination = source.with_name(source.name.replace("588080", "159352"))
        if source.suffix == ".json":
            destination.write_text(
                source.read_text(encoding="utf-8")
                .replace("588080.SH", "159352.SZ")
                .replace("588080", "159352"),
                encoding="utf-8",
            )
        else:
            shutil.copy2(source, destination)
    generalized = invoke_main(
        [
            "backtest", "run",
            "--strategy", "S001", "--strategy-version", "v1",
            "--dataset", "backtest", "--symbol", "159352.SZ", "--asset", "etf",
            "--start", "2026-01-01", "--end", "2026-09-02", "--init-cash", "100000",
            "--outputs-root", str(functional_repo / "outputs"),
            "--repo-root", str(functional_repo),
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
    }
    generalized_report = (generalized_dir / "report.md").read_text(encoding="utf-8")
    assert "- 策略参考标的：588080.SH" in generalized_report
    assert "- 实际回测标的：159352.SZ" in generalized_report
    assert "- 应用方式：跨标的泛化测试" in generalized_report
