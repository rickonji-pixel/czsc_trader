from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import shutil
import tempfile

import plotly.graph_objects as go
from strategy_evaluator import AuditStatus, audit_replay

from czsc_trader.reporting.publication import publish_run_directory

from .datasets import DatasetName, ReplayData
from .audit_adapter import build_replay_evidence
from .evidence import build_manifest
from .execution_replay import replay_account
from .metrics import calculate_metrics
from .models import StrategySnapshot
from .report import render_report
from .signal_replay import replay_signals


@dataclass(frozen=True)
class BacktestRequestV2:
    symbol: str
    asset_type: str
    dataset: DatasetName
    start: date
    end: date
    initial_cash: float


@dataclass(frozen=True)
class BacktestRunSummary:
    output_dir: Path
    metrics: dict[str, object]
    manifest: dict[str, object]


def _chart_html(result: object, reference: str) -> str:
    account = result.account_daily
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(x=account["date"], y=account["equity"], name="账户权益", line={"width": 2})
    )
    figure.update_layout(
        template="plotly_dark",
        title=f"{reference} 确定性回测",
        xaxis_title="交易日",
        yaxis_title="账户权益",
        height=560,
        margin={"l": 70, "r": 30, "t": 70, "b": 60},
    )
    return figure.to_html(full_html=True, include_plotlyjs=True)


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def run_backtest_v2(
    *,
    snapshot: StrategySnapshot,
    replay_data: ReplayData,
    request: BacktestRequestV2,
    outputs_root: Path,
    run_date: date,
) -> BacktestRunSummary:
    """Run, validate, and atomically publish one immutable replay."""
    if request.dataset != replay_data.dataset:
        raise ValueError("request dataset differs from loaded replay data")
    signals = replay_signals(snapshot, replay_data, request.start, request.end)
    result = replay_account(signals, replay_data, request.initial_cash)
    metrics = calculate_metrics(result, request.initial_cash)
    evidence = build_replay_evidence(
        signals, replay_data, result, request.initial_cash, metrics
    )
    audited = audit_replay(evidence)
    if audited.status is not AuditStatus.PASS:
        raise ValueError(f"SE replay audit failed: {', '.join(audited.reason_codes)}")
    audit = audited.to_dict()
    manifest = build_manifest(
        request=request,
        snapshot=snapshot,
        data=replay_data,
        signals=signals,
        metrics=metrics,
        audit=audit,
        run_date=run_date,
    )
    root = Path(outputs_root)
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".backtest_v2_", dir=root))
    try:
        for name, frame in (
            ("decisions.csv", result.decisions),
            ("orders.csv", result.orders),
            ("fills.csv", result.fills),
            ("account_daily.csv", result.account_daily),
            ("trades.csv", result.trades),
        ):
            frame.to_csv(staging / name, index=False, encoding="utf-8-sig", lineterminator="\n")
        _write_json(staging / "metrics.json", metrics)
        _write_json(staging / "audit.json", audit)
        _write_json(staging / "manifest.json", manifest)
        (staging / "report.md").write_text(render_report(snapshot, metrics), encoding="utf-8")
        (staging / "chart.html").write_text(
            _chart_html(result, snapshot.identity.reference), encoding="utf-8"
        )
        expected = {
            "manifest.json", "decisions.csv", "orders.csv", "fills.csv",
            "account_daily.csv", "trades.csv", "metrics.json", "audit.json",
            "report.md", "chart.html",
        }
        if {item.name for item in staging.iterdir()} != expected:
            raise AssertionError("backtest publication is structurally incomplete")
        output_dir = publish_run_directory(staging, root, request.symbol, run_date)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return BacktestRunSummary(output_dir, metrics, manifest)
