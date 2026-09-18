from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
import json
from pathlib import Path
import shutil

import pandas as pd
from strategy_evaluator import AuditStatus, audit_benchmark_replay, audit_replay

from czsc_trader.reporting.publication import publish_run_directory
from czsc_trader.ma_charting import write_ma_chart
from czsc_trader.temp_workspace import create_temporary_directory

from .benchmarks import replay_benchmarks
from .datasets import DatasetName, ReplayData
from .audit_adapter import build_benchmark_evidence, build_replay_evidence
from .chart import render_backtest_chart_html
from .causal_feature_gate_replay import build_causal_feature_gate_signals
from .closing_dislocation_replay import build_closing_dislocation_signals
from .evidence import build_manifest
from .execution_replay import replay_account
from .intraday_overlay_replay import (
    build_moneyflow_breadth_signals,
    replay_intraday_overlay,
)
from .metrics import calculate_metrics
from .models import StrategySnapshot
from .report import render_report
from .signal_replay import replay_signals
from .srt_bridge import (
    build_srt_signal_replay,
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


def _bind_backtest_symbol(
    snapshot: StrategySnapshot,
    request: BacktestRequestV2,
) -> tuple[StrategySnapshot, dict[str, str]]:
    resolved_rule = snapshot.resolved_rule
    if resolved_rule is None:
        raise ValueError("candidate backtest requires a resolved research rule")
    spec = resolved_rule.execution
    overlay = resolved_rule.constituent_moneyflow_intraday
    closing_dislocation = resolved_rule.closing_dislocation_overnight
    causal_feature_gate = resolved_rule.causal_feature_gate
    if causal_feature_gate is not None:
        requested_symbol = request.symbol.upper()
        reference_symbol = causal_feature_gate.symbol.upper()
        if request.asset_type != "etf":
            raise ValueError("causal-feature-gate strategy requires an ETF")
        if requested_symbol != reference_symbol:
            raise ValueError("causal-feature-gate strategy cannot be rebound to another symbol")
        return snapshot, {
            "mode": "native_symbol",
            "strategy_reference_symbol": reference_symbol,
            "backtest_symbol": requested_symbol,
        }
    if closing_dislocation is not None:
        requested_symbol = request.symbol.upper()
        reference_symbol = closing_dislocation.symbol.upper()
        if request.asset_type != "etf":
            raise ValueError("closing-dislocation strategy requires an ETF")
        if requested_symbol != reference_symbol:
            raise ValueError("closing-dislocation strategy cannot be rebound to another symbol")
        return snapshot, {
            "mode": "native_symbol",
            "strategy_reference_symbol": reference_symbol,
            "backtest_symbol": requested_symbol,
        }
    if overlay is not None:
        requested_symbol = request.symbol.upper()
        reference_symbol = overlay.symbol.upper()
        if request.asset_type != "etf":
            raise ValueError("constituent-moneyflow intraday strategy requires an ETF")
        if requested_symbol != reference_symbol:
            raise ValueError(
                "constituent-moneyflow intraday strategy cannot be rebound to another symbol"
            )
        return snapshot, {
            "mode": "native_symbol",
            "strategy_reference_symbol": reference_symbol,
            "backtest_symbol": requested_symbol,
        }
    if spec is None:
        raise ValueError("strategy snapshot has no execution specification")
    requested_symbol = request.symbol.upper()
    if spec.instrument.asset_type != request.asset_type:
        raise ValueError("requested asset type differs from strategy execution specification")
    reference_symbol = spec.instrument.symbol.upper()
    instrument = replace(spec.instrument, symbol=requested_symbol)
    execution = replace(spec, instrument=instrument)
    resolved_rule = replace(
        resolved_rule,
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
    repository_root: Path | None = None,
) -> BacktestRunSummary:
    """Run, validate, and atomically publish one immutable replay."""
    if request.dataset != replay_data.dataset:
        raise ValueError("request dataset differs from loaded replay data")
    request = replace(request, symbol=request.symbol.upper())
    if replay_data.adjusted.symbol != request.symbol:
        raise ValueError("request symbol differs from loaded replay data")
    if replay_data.adjusted.asset_type != request.asset_type:
        raise ValueError("request asset type differs from loaded replay data")
    if snapshot.identity.kind == "REGISTERED":
        if repository_root is None:
            raise ValueError("registered strategy backtest requires a repository root")
        _, frozen_strategy = load_srt_strategy(
            repository_root, snapshot.identity.reference
        )
        reference_symbol = strategy_reference_symbol(frozen_strategy)
        applied_snapshot = snapshot
        application = {
            "mode": (
                "native_symbol"
                if reference_symbol == request.symbol
                else "cross_symbol_generalization"
            ),
            "strategy_reference_symbol": reference_symbol,
            "backtest_symbol": request.symbol,
        }
        strategy, signals = build_srt_signal_replay(
            snapshot=applied_snapshot,
            replay_data=replay_data,
            start=pd.Timestamp(request.start),
            end=pd.Timestamp(request.end),
            repository_root=repository_root,
        )
        result = replay_srt_account(
            strategy=strategy,
            signals=signals,
            replay_data=replay_data,
            initial_cash=request.initial_cash,
        )
        application["runtime_engine"] = "srt"
    else:
        applied_snapshot, application = _bind_backtest_symbol(snapshot, request)
        resolved_rule = applied_snapshot.resolved_rule
        if resolved_rule is None:
            raise ValueError("candidate backtest requires a resolved research rule")
        if resolved_rule.causal_feature_gate is not None:
            if repository_root is None:
                raise ValueError("causal-feature candidate requires a repository root")
            signals = build_causal_feature_gate_signals(
                applied_snapshot,
                replay_data,
                pd.Timestamp(request.start),
                pd.Timestamp(request.end),
                repository_root,
            )
            result = replay_account(signals, replay_data, request.initial_cash)
        elif resolved_rule.constituent_moneyflow_intraday is not None:
            if repository_root is None:
                raise ValueError("intraday-overlay candidate requires a repository root")
            signals = build_moneyflow_breadth_signals(
                applied_snapshot,
                replay_data,
                repository_root,
                pd.Timestamp(request.start),
                pd.Timestamp(request.end),
            )
            result = replay_intraday_overlay(signals, replay_data, request.initial_cash)
        elif resolved_rule.closing_dislocation_overnight is not None:
            signals = build_closing_dislocation_signals(
                applied_snapshot,
                replay_data,
                pd.Timestamp(request.start),
                pd.Timestamp(request.end),
                repository_root,
            )
            result = replay_account(signals, replay_data, request.initial_cash)
        else:
            signals = replay_signals(
                applied_snapshot, replay_data, request.start, request.end
            )
            result = replay_account(signals, replay_data, request.initial_cash)
        application["runtime_engine"] = "research_candidate"
    strategy_metrics = calculate_metrics(result, request.initial_cash)
    evidence = build_replay_evidence(
        signals, replay_data, result, request.initial_cash, strategy_metrics
    )
    audited = audit_replay(evidence)
    if audited.status is not AuditStatus.PASS:
        raise ValueError(f"SE replay audit failed: {', '.join(audited.reason_codes)}")
    audit = audited.to_dict()
    benchmarks = replay_benchmarks(signals, replay_data, request.initial_cash)
    benchmark_audits = {
        name: audit_benchmark_replay(evidence)
        for name, evidence in build_benchmark_evidence(
            benchmarks, signals, replay_data, request.initial_cash
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
        data=replay_data,
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
            render_backtest_chart_html(signals, replay_data, result, request.initial_cash),
            encoding="utf-8",
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
        output_dir = publish_run_directory(staging, root, request.symbol, run_date)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return BacktestRunSummary(output_dir, metrics, manifest)
