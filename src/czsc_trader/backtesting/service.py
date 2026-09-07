from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
import json
from pathlib import Path
import shutil
import tempfile

from strategy_evaluator import AuditStatus, audit_replay

from czsc_trader.reporting.publication import publish_run_directory
from czsc_trader.ma_charting import write_ma_chart

from .benchmarks import replay_benchmarks
from .datasets import DatasetName, ReplayData
from .audit_adapter import build_replay_evidence
from .chart import render_backtest_chart_html
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


def _bind_backtest_symbol(
    snapshot: StrategySnapshot,
    request: BacktestRequestV2,
) -> tuple[StrategySnapshot, dict[str, str]]:
    spec = snapshot.resolved_rule.execution
    if spec is None:
        raise ValueError("strategy snapshot has no execution specification")
    requested_symbol = request.symbol.upper()
    if spec.instrument.asset_type != request.asset_type:
        raise ValueError("requested asset type differs from strategy execution specification")
    reference_symbol = spec.instrument.symbol.upper()
    instrument = replace(spec.instrument, symbol=requested_symbol)
    execution = replace(spec, instrument=instrument)
    resolved_rule = replace(
        snapshot.resolved_rule,
        symbol=requested_symbol,
        execution=execution,
    )
    application = {
        "mode": (
            "native_symbol"
            if reference_symbol == requested_symbol
            else "cross_symbol_generalization"
        ),
        "strategy_reference_symbol": reference_symbol,
        "backtest_symbol": requested_symbol,
    }
    return replace(snapshot, resolved_rule=resolved_rule), application


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
    request = replace(request, symbol=request.symbol.upper())
    if replay_data.adjusted.symbol != request.symbol:
        raise ValueError("request symbol differs from loaded replay data")
    if replay_data.adjusted.asset_type != request.asset_type:
        raise ValueError("request asset type differs from loaded replay data")
    applied_snapshot, application = _bind_backtest_symbol(snapshot, request)
    signals = replay_signals(applied_snapshot, replay_data, request.start, request.end)
    result = replay_account(signals, replay_data, request.initial_cash)
    strategy_metrics = calculate_metrics(result, request.initial_cash)
    evidence = build_replay_evidence(
        signals, replay_data, result, request.initial_cash, strategy_metrics
    )
    audited = audit_replay(evidence)
    if audited.status is not AuditStatus.PASS:
        raise ValueError(f"SE replay audit failed: {', '.join(audited.reason_codes)}")
    audit = audited.to_dict()
    benchmarks = replay_benchmarks(signals, replay_data, request.initial_cash)
    metrics = {
        "strategy": {
            "reference": snapshot.identity.reference,
            "metrics": strategy_metrics,
        },
        "benchmarks": benchmarks.metrics,
    }
    manifest = build_manifest(
        request=request,
        snapshot=snapshot,
        data=replay_data,
        signals=signals,
        metrics=metrics,
        audit=audit,
        application=application,
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
            ("buyhold_account_daily.csv", benchmarks.buyhold_account_daily),
            ("ma_signals.csv", benchmarks.ma_signals),
            ("ma_orders.csv", benchmarks.ma_orders),
            ("ma_account_daily.csv", benchmarks.ma_account_daily),
            ("ma_trades.csv", benchmarks.ma_trades),
        ):
            frame.to_csv(staging / name, index=False, encoding="utf-8-sig", lineterminator="\n")
        _write_json(staging / "metrics.json", metrics)
        _write_json(staging / "audit.json", audit)
        _write_json(staging / "manifest.json", manifest)
        (staging / "report.md").write_text(
            render_report(
                snapshot,
                metrics,
                strategy_reference_symbol=application["strategy_reference_symbol"],
                backtest_symbol=application["backtest_symbol"],
                application_mode=application["mode"],
                calculation_start=signals.calculation_start.date(),
                calculation_end=signals.calculation_end.date(),
                evaluation_start=signals.evaluation_start.date(),
                evaluation_end=signals.evaluation_end.date(),
                trading_days=len(result.account_daily),
            ),
            encoding="utf-8",
        )
        (staging / "chart.html").write_text(
            render_backtest_chart_html(signals, replay_data, result), encoding="utf-8"
        )
        ma_chart_signals = benchmarks.ma_signals.set_index("date")
        write_ma_chart(
            replay_data.adjusted.daily,
            ma_chart_signals,
            benchmarks.ma_orders,
            signals.evaluation_start,
            signals.evaluation_end,
            f"{snapshot.identity.reference}｜MA5/MA20基准",
            staging / "ma_chart.html",
        )
        expected = {
            "manifest.json", "decisions.csv", "orders.csv", "fills.csv",
            "account_daily.csv", "trades.csv", "metrics.json", "audit.json",
            "report.md", "chart.html", "buyhold_account_daily.csv",
            "ma_signals.csv", "ma_orders.csv", "ma_account_daily.csv",
            "ma_trades.csv", "ma_chart.html",
        }
        if {item.name for item in staging.iterdir()} != expected:
            raise AssertionError("backtest publication is structurally incomplete")
        output_dir = publish_run_directory(staging, root, request.symbol, run_date)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return BacktestRunSummary(output_dir, metrics, manifest)
