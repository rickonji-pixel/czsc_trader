from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
import json
from pathlib import Path
import shutil

from strategy_evaluator import AuditStatus, audit_benchmark_replay, audit_replay

from czsc_trader.reporting.publication import publish_run_directory
from czsc_trader.ma_charting import write_ma_chart
from czsc_trader.temp_workspace import create_temporary_directory

from .benchmarks import replay_benchmarks
from .datasets import DatasetName
from .execution_data import BacktestExecutionData, prepare_backtest_execution_data
from .audit_adapter import build_benchmark_evidence, build_replay_evidence
from .chart import render_backtest_chart_html
from .evidence import build_manifest
from .metrics import calculate_metrics
from .models import StrategySnapshot
from .report import render_report
from .srt_bridge import (
    build_srt_signal_replay,
    describe_snapshot_strategy,
    execution_intraday_frequencies,
    load_srt_strategy,
    replay_srt_account,
    strategy_reference_symbol,
)


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


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def run_backtest_v2(
    *,
    snapshot: StrategySnapshot,
    request: BacktestRequestV2,
    data_dir: Path,
    outputs_root: Path,
    run_date: date,
    repository_root: Path | None = None,
    execution_data: BacktestExecutionData | None = None,
) -> BacktestRunSummary:
    """Run, validate, and atomically publish one immutable replay."""
    request = replace(request, symbol=request.symbol.upper())
    if repository_root is None:
        raise ValueError("SRT backtest requires a repository root")
    _, definition = describe_snapshot_strategy(
        repository_root,
        snapshot,
        deployment_symbol=request.symbol,
    )
    if execution_data is None:
        execution_data = prepare_backtest_execution_data(
            dataset=request.dataset,
            data_dir=data_dir,
            symbol=request.symbol,
            asset_type=request.asset_type,
            start=request.start,
            end=request.end,
            env_file=Path(repository_root) / ".env",
            include_five_minute="5m" in execution_intraday_frequencies(definition),
        )
    if request.dataset != execution_data.dataset:
        raise ValueError("request dataset differs from TDR execution data")
    if execution_data.symbol != request.symbol:
        raise ValueError("request symbol differs from TDR execution data")
    if execution_data.asset_type != request.asset_type:
        raise ValueError("request asset type differs from TDR execution data")
    strategy, signals = build_srt_signal_replay(
        snapshot=snapshot,
        execution_data=execution_data,
        start=execution_data.evaluation_start,
        end=execution_data.evaluation_end,
        repository_root=repository_root,
    )
    reference_symbol = strategy_reference_symbol(strategy)
    if snapshot.identity.kind == "REGISTERED":
        # The calculation runtime may be rebound; preserve the native symbol in reports.
        _, native_strategy = load_srt_strategy(repository_root, snapshot.identity.reference)
        reference_symbol = strategy_reference_symbol(native_strategy)
    application = {
        "mode": "native_symbol" if reference_symbol == request.symbol else "cross_symbol_generalization",
        "strategy_reference_symbol": reference_symbol,
        "backtest_symbol": request.symbol,
        "runtime_engine": "srt",
    }
    result = replay_srt_account(
        strategy=strategy, signals=signals, execution_data=execution_data,
        initial_cash=request.initial_cash,
    )
    strategy_metrics = calculate_metrics(result, request.initial_cash)
    evidence = build_replay_evidence(
        signals, execution_data, result, request.initial_cash, strategy_metrics
    )
    audited = audit_replay(evidence)
    if audited.status is not AuditStatus.PASS:
        raise ValueError(f"SE replay audit failed: {', '.join(audited.reason_codes)}")
    audit = audited.to_dict()
    benchmarks = replay_benchmarks(signals, execution_data, request.initial_cash)
    benchmark_audits = {
        name: audit_benchmark_replay(evidence)
        for name, evidence in build_benchmark_evidence(
            benchmarks, signals, execution_data, request.initial_cash
        ).items()
    }
    failures = {
        name: result.reason_codes
        for name, result in benchmark_audits.items()
        if result.status is not AuditStatus.PASS
    }
    if failures:
        raise ValueError(f"SE benchmark audit failed: {failures}")
    audit["benchmarks"] = {
        name: result.to_dict() for name, result in benchmark_audits.items()
    }
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
        data=execution_data,
        signals=signals,
        metrics=metrics,
        audit=audit,
        application=application,
        run_date=run_date,
    )
    root = Path(outputs_root)
    root.mkdir(parents=True, exist_ok=True)
    staging = create_temporary_directory(root, "backtest", prefix="run-")
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
                research_start=snapshot.research_start,
                research_end=snapshot.research_end,
                calculation_start=signals.calculation_start.date(),
                calculation_end=signals.calculation_end.date(),
                evaluation_start=signals.evaluation_start.date(),
                evaluation_end=signals.evaluation_end.date(),
                trading_days=len(result.account_daily),
            ),
            encoding="utf-8",
        )
        (staging / "chart.html").write_text(
            render_backtest_chart_html(signals, execution_data, result, request.initial_cash),
            encoding="utf-8",
        )
        ma_chart_signals = benchmarks.ma_signals.set_index("date")
        write_ma_chart(
            execution_data.adjusted_daily,
            ma_chart_signals,
            benchmarks.ma_orders,
            signals.evaluation_start,
            signals.evaluation_end,
            f"{snapshot.identity.reference}｜MA5/MA20基准",
            staging / "ma_chart.html",
        )
        expected = {
            "manifest.json",
            "decisions.csv",
            "orders.csv",
            "fills.csv",
            "account_daily.csv",
            "trades.csv",
            "metrics.json",
            "audit.json",
            "report.md",
            "chart.html",
            "buyhold_account_daily.csv",
            "ma_signals.csv",
            "ma_orders.csv",
            "ma_account_daily.csv",
            "ma_trades.csv",
            "ma_chart.html",
        }
        if {item.name for item in staging.iterdir()} != expected:
            raise AssertionError("backtest publication is structurally incomplete")
        output_dir = publish_run_directory(
            staging,
            root,
            snapshot.identity.reference,
            request.symbol,
            run_date,
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return BacktestRunSummary(output_dir, metrics, manifest)
