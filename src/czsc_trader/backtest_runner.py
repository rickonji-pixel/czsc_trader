"""Orchestrate fixed-baseline backtests without candidate research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path
import platform

import pandas as pd

from .audit import audit_no_lookahead
from .backtest import PeriodBacktestResult, run_backtest, run_period_backtests
from .baseline_execution import apply_resolved_baseline
from .baselines import ExecutionSpec, resolve_baseline
from .charting import write_period_chart
from .data import load_market_data
from .execution_policy import (
    ExecutionSimulation,
    entry_limit_series,
    policy_metrics,
    simulate_limit_policy,
)
from .factors import generate_factor_frame
from .ma_charting import write_ma_chart
from .moving_average import moving_average_signals
from .output_paths import create_output_dir
from .reporting.backtest_report import render_backtest_report
from .strategy_metrics import strategy_comparison_metrics


@dataclass(frozen=True)
class BacktestRequest:
    symbol: str
    asset_type: str
    start: date | None = None
    end: date | None = None
    baseline: str | None = None
    windows_path: Path | None = None
    window: str | None = None
    fee_rate: float = 0.0005
    init_cash: float = 1_000_000.0
    raw_dir: Path = Path("data/raw")
    outputs_root: Path = Path("outputs")
    baseline_root: Path = Path("configs/rule_baselines")
    execution_policy_root: Path = Path("configs/execution_policies")


def _json_default(value: object) -> object:
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _write_csv(path: Path, frame: pd.DataFrame, *, index: bool = False) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=index, encoding="utf-8-sig")
    temporary.replace(path)


def _load_window_config(
    path: Path,
    available_dates: pd.Series,
) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    def parse_fixed_boundary(value: str, name: object, boundary: str) -> pd.Timestamp:
        try:
            timestamp = pd.Timestamp(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"backtest window {name} has invalid {boundary}"
            ) from exc
        if pd.isna(timestamp):
            raise ValueError(f"backtest window {name} has invalid {boundary}")
        return timestamp

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read backtest window config {path}: {exc}") from exc
    periods_payload = payload.get("periods") if isinstance(payload, dict) else None
    if not isinstance(periods_payload, dict) or not periods_payload:
        raise ValueError("backtest window config periods must be a non-empty object")
    periods: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {}
    for name, item in periods_payload.items():
        if not isinstance(item, dict):
            raise ValueError(f"backtest window {name} must be an object")
        try:
            start_raw = item["start"]
            end_raw = item["end"]
        except KeyError as exc:
            raise ValueError(f"backtest window {name} requires start and end") from exc
        start_value = str(start_raw)
        end_value = str(end_raw)
        dates = pd.DatetimeIndex(available_dates).normalize()
        dynamic_values = {"first_available_in_year", "last_available_in_year"}
        if start_value in dynamic_values or end_value in dynamic_values:
            try:
                year = int(item["year"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"backtest window {name} requires integer year for dynamic boundaries"
                ) from exc
            candidates = dates[dates.year == year]
            if candidates.empty:
                raise ValueError(f"backtest window {name} has no market data in {year}")
        start = (
            candidates.min()
            if start_value == "first_available_in_year"
            else parse_fixed_boundary(start_value, name, "start")
        )
        end = (
            candidates.max()
            if end_value == "last_available_in_year"
            else parse_fixed_boundary(end_value, name, "end")
        )
        if start > end:
            raise ValueError(f"backtest window {name} starts after it ends")
        periods[str(name)] = (start, end)
    return periods


def _equity_frame(
    result: PeriodBacktestResult,
    target_position: pd.Series,
    factor_score: pd.Series,
) -> pd.DataFrame:
    index = pd.DatetimeIndex(result.equity.index, name="dt")
    return pd.DataFrame(
        {
            "dt": index,
            "equity": result.equity.to_numpy(),
            "target_position": target_position.reindex(index).to_numpy(),
            "execution_position": target_position.reindex(index).shift(1).to_numpy(),
            "factor_score": factor_score.reindex(index).to_numpy(),
        }
    )


def _ma_equity_frame(
    result: PeriodBacktestResult,
    target_position: pd.Series,
    signals: pd.DataFrame,
) -> pd.DataFrame:
    index = pd.DatetimeIndex(result.equity.index, name="dt")
    prior = target_position.index[target_position.index < index[0]]
    if prior.empty:
        raise ValueError("MA backtest period has no prior signal")
    execution = target_position.reindex(index).shift(1)
    execution.iloc[0] = float(target_position.loc[prior[-1]])
    return pd.DataFrame(
        {
            "dt": index,
            "equity": result.equity.to_numpy(),
            "target_position": target_position.reindex(index).to_numpy(),
            "execution_position": execution.to_numpy(),
            "ma5": signals["ma5"].reindex(index).to_numpy(),
            "ma20": signals["ma20"].reindex(index).to_numpy(),
        }
    )


def _strategy_metrics(
    daily: pd.DataFrame,
    active: PeriodBacktestResult,
    ma: PeriodBacktestResult,
    *,
    fee_rate: float,
    init_cash: float,
    execution: ExecutionSimulation | None = None,
) -> dict[str, object]:
    """Build one independently funded strategy comparison."""
    start = pd.Timestamp(str(active.metrics["start"]))
    end = pd.Timestamp(str(active.metrics["end"]))
    dates = pd.to_datetime(daily["dt"])
    period_daily = daily.loc[(dates >= start) & (dates <= end)].copy()
    buyhold_target = pd.Series(
        1.0,
        index=pd.DatetimeIndex(pd.to_datetime(period_daily["dt"]), name="dt"),
        name="buyhold_target",
    )
    buyhold = run_backtest(
        period_daily,
        buyhold_target,
        fee_rate=fee_rate,
        init_cash=init_cash,
        initial_target=1.0,
    )
    strategies = {
        "active_baseline": strategy_comparison_metrics(
            active.equity, active.orders, init_cash
        ),
        "buyhold": strategy_comparison_metrics(
            buyhold.equity, buyhold.orders, init_cash
        ),
        "ma5_ma20": strategy_comparison_metrics(
            ma.equity, ma.orders, init_cash
        ),
    }
    if execution is not None:
        values = policy_metrics(execution, init_cash)
        strategies["active_baseline_execution"] = {
            key: values[key]
            for key in (
                "max_drawdown",
                "calmar",
                "win_loss_ratio",
                "win_loss_ratio_status",
                "return",
                "sharpe",
            )
        }
    return {
        "start": str(active.metrics["start"]),
        "end": str(active.metrics["end"]),
        "strategies": strategies,
    }


def _complete_baseline_execution_results(
    daily: pd.DataFrame,
    intraday: pd.DataFrame,
    target_position: pd.Series,
    periods: dict[str, tuple[pd.Timestamp, pd.Timestamp]],
    execution: ExecutionSpec,
    *,
    fee_rate: float,
    init_cash: float,
    slippage_bp: int = 0,
) -> dict[str, ExecutionSimulation]:
    """Run one independently funded execution simulation per requested window."""
    daily_dates = pd.DatetimeIndex(pd.to_datetime(daily["dt"]), name="dt")
    if execution.entry_limit_family != "previous_close_ratio":
        raise ValueError("unsupported complete-baseline entry limit family")
    limits = entry_limit_series(
        daily,
        "fixed",
        execution.entry_limit_parameter,
        tick=execution.instrument.price_tick,
    )
    output: dict[str, ExecutionSimulation] = {}
    for name, (start, end) in periods.items():
        prior = daily_dates[daily_dates < start]
        if prior.empty:
            raise ValueError(f"execution-policy window {name} has no prior signal session")
        simulation_start = prior[-1]
        daily_mask = pd.Series(daily_dates, index=daily.index).between(simulation_start, end)
        period_daily = daily.loc[daily_mask].copy()
        intraday_dates = pd.to_datetime(intraday["dt"]).dt.normalize()
        period_intraday = intraday.loc[
            intraday_dates.between(simulation_start.normalize(), end.normalize())
        ].copy()
        period_index = pd.DatetimeIndex(pd.to_datetime(period_daily["dt"]), name="dt")
        simulation = simulate_limit_policy(
            period_daily,
            period_intraday,
            target_position.reindex(period_index),
            limits.reindex(period_index),
            fee_rate=fee_rate,
            init_cash=init_cash,
            diagnostic_quantity=execution.instrument.maximum_order_quantity,
            lot_size=execution.instrument.lot_size,
            fill_on_equal_touch=False,
            slippage_bp=slippage_bp,
        )
        execution_dates = pd.to_datetime(simulation.orders["execution_date"])
        orders = simulation.orders.loc[
            execution_dates.dt.normalize().between(start.normalize(), end.normalize())
        ].copy().reset_index(drop=True)
        output[name] = ExecutionSimulation(
            simulation.equity.loc[start:end].copy(),
            orders,
            simulation.daily_state.loc[start:end].copy(),
            simulation.cycles.copy(),
            {},
        )
    return output


def run_fixed_backtest(
    request: BacktestRequest,
    *,
    run_date: date | None = None,
) -> dict[str, object]:
    """Run one already-frozen rule against one validated symbol."""
    if request.windows_path is not None and (request.start is not None or request.end is not None):
        raise ValueError("--windows cannot be combined with --start or --end")
    if request.window is not None and request.windows_path is None:
        raise ValueError("--window requires --windows")
    effective_date = run_date or datetime.now().astimezone().date()
    output_dir = create_output_dir(request.outputs_root, request.symbol, effective_date)
    try:
        baseline = resolve_baseline(
            request.baseline_root,
            request.baseline,
            symbol=request.symbol,
        )
        execution = baseline.execution
        effective_fee_rate = (
            execution.capital.fee_rate if execution is not None else float(request.fee_rate)
        )
        if execution is not None and abs(float(request.fee_rate) - effective_fee_rate) > 1e-12:
            raise ValueError(
                "backtest fee rate must equal the complete baseline fee rate "
                f"({effective_fee_rate})"
            )
        data = load_market_data(request.raw_dir, request.symbol, request.asset_type)
        if request.windows_path is not None:
            periods = _load_window_config(
                request.windows_path,
                data.daily["dt"],
            )
            if request.window is not None:
                if request.window not in periods:
                    raise ValueError(f"unknown backtest window: {request.window}")
                periods = {request.window: periods[request.window]}
            cutoff = max(end for _, end in periods.values())
        else:
            start = pd.Timestamp(request.start or data.daily["dt"].min().date())
            end = pd.Timestamp(request.end or data.daily["dt"].max().date())
            if start > end:
                raise ValueError("backtest start must not be after end")
            periods = {"full": (start, end)}
            cutoff = end
        causal_data = data.truncate(cutoff)
        factor_result = generate_factor_frame(causal_data)
        daily_close = pd.Series(
            causal_data.daily["close"].astype(float).to_numpy(),
            index=pd.DatetimeIndex(pd.to_datetime(causal_data.daily["dt"]), name="dt"),
            name="close",
        )
        applied = apply_resolved_baseline(
            factor_result.frame,
            baseline,
            daily_close=daily_close,
        )
        factor_output = factor_result.frame.copy()
        factor_output.insert(0, "target_position", applied.target_position)
        factor_output.insert(1, "factor_score", applied.scores)
        factor_output.insert(2, "enter_threshold", float(baseline.rule.enter))
        factor_output.insert(3, "exit_threshold", float(baseline.rule.exit))
        factor_output.index.name = "dt"

        results = run_period_backtests(
            causal_data.daily,
            applied.target_position,
            periods,
            fee_rate=request.fee_rate,
            init_cash=request.init_cash,
            factor_events=applied.events,
            factor_frame=factor_output,
        )
        ma_signals = moving_average_signals(causal_data.daily, fast=5, slow=20)
        ma_results = run_period_backtests(
            causal_data.daily,
            ma_signals["target_position"],
            periods,
            fee_rate=request.fee_rate,
            init_cash=request.init_cash,
        )
        execution_results = (
            _complete_baseline_execution_results(
                causal_data.daily,
                causal_data.intraday,
                applied.target_position,
                periods,
                execution,
                fee_rate=effective_fee_rate,
                init_cash=request.init_cash,
            )
            if execution is not None
            else {}
        )
        order_pieces = [result.orders for result in results.values()]
        orders = pd.concat(order_pieces, ignore_index=True) if order_pieces else pd.DataFrame()
        event_pieces = [applied.events, *[result.factor_events for result in results.values()]]
        nonempty_events = [piece for piece in event_pieces if not piece.empty]
        factor_events = (
            pd.concat(nonempty_events, ignore_index=True)
            .drop_duplicates(subset=["event_id"])
            .sort_values("signal_date")
            .reset_index(drop=True)
            if nonempty_events
            else pd.DataFrame()
        )
        audit = audit_no_lookahead(
            orders,
            factor_events,
            applied.target_position,
            factor_output,
        )
        windows = {
            name: _strategy_metrics(
                causal_data.daily,
                result,
                ma_results[name],
                fee_rate=request.fee_rate,
                init_cash=request.init_cash,
                execution=execution_results.get(name),
            )
            for name, result in results.items()
        }
        metrics: dict[str, object] = {
            "windows": windows,
        }

        single = list(results) == ["full"]
        chart_files: list[str] = []
        for name, result in results.items():
            suffix = "" if single else f"_{name}"
            _write_csv(output_dir / f"orders{suffix}.csv", result.orders)
            _write_csv(
                output_dir / f"equity{suffix}.csv",
                _equity_frame(result, applied.target_position, applied.scores),
            )
            chart_name = f"chart{suffix}.html"
            write_period_chart(
                causal_data.daily,
                factor_output,
                result.orders,
                pd.Timestamp(str(result.metrics["start"])),
                pd.Timestamp(str(result.metrics["end"])),
                f"{data.symbol} {name} · 固定基线 {baseline.version}",
                output_dir / chart_name,
            )
            chart_files.append(chart_name)
            ma_result = ma_results[name]
            _write_csv(output_dir / f"ma_orders{suffix}.csv", ma_result.orders)
            _write_csv(
                output_dir / f"ma_equity{suffix}.csv",
                _ma_equity_frame(
                    ma_result,
                    ma_signals["target_position"],
                    ma_signals,
                ),
            )
            ma_chart_name = f"ma_chart{suffix}.html"
            write_ma_chart(
                causal_data.daily,
                ma_signals,
                ma_result.orders,
                pd.Timestamp(str(ma_result.metrics["start"])),
                pd.Timestamp(str(ma_result.metrics["end"])),
                f"{data.symbol} {name} · MA5/MA20",
                output_dir / ma_chart_name,
            )
            chart_files.append(ma_chart_name)
            execution_result = execution_results.get(name)
            if execution_result is not None:
                _write_csv(
                    output_dir / f"execution_orders{suffix}.csv",
                    execution_result.orders,
                )
                _write_csv(
                    output_dir / f"execution_equity{suffix}.csv",
                    execution_result.daily_state.reset_index(),
                )

        _write_csv(output_dir / "factors.csv", factor_output, index=True)
        _write_csv(output_dir / "ma_signals.csv", ma_signals, index=True)
        _write_csv(output_dir / "factor_events.csv", factor_events)
        _write_json(output_dir / "audit.json", audit)
        _write_json(output_dir / "metrics.json", metrics)
        _write_json(output_dir / "baseline_rule.json", baseline.rule_payload)
        manifest = {
            "run_type": "fixed_baseline_backtest",
            "run_at_utc": datetime.now(timezone.utc).isoformat(),
            "symbol": data.symbol,
            "asset_type": data.asset_type,
            "fee_rate_per_side": effective_fee_rate,
            "initial_cash": request.init_cash,
            "metrics_schema_version": 5,
            "comparison_strategies": {
                "active_baseline": baseline.version,
                "buyhold": {"initial_target": 1.0},
                "ma5_ma20": {
                    "fast": 5,
                    "slow": 20,
                    "execution": "next_session_open",
                    "position": "full_or_cash",
                },
                **(
                    {
                        "active_baseline_execution": {
                            "baseline_version": baseline.version,
                            "baseline_sha256": baseline.sha256,
                            "execution": "embedded_complete_baseline",
                        }
                    }
                    if execution is not None
                    else {}
                ),
            },
            "baseline": {
                "version": baseline.version,
                "sha256": baseline.sha256,
                "strategy": baseline.strategy,
                "status": baseline.status,
                "scope": baseline.scope,
                "symbol": baseline.symbol,
                "source_path": baseline.source_path,
                "source_sha256": baseline.source_sha256,
                "selection_sample_end": baseline.selection_sample_end,
                "forward_validation_start": baseline.forward_validation_start,
                "rule": baseline.rule_payload,
            },
            "data": {
                "manifest": data.manifest,
                "hashes": data.hashes,
                "cutoff": str(causal_data.daily["dt"].max().date()),
            },
            "periods": {
                name: {"start": str(start.date()), "end": str(end.date())}
                for name, (start, end) in periods.items()
            },
            "charts": chart_files,
            "audit_status": audit["status"],
            "versions": {
                "python": platform.python_version(),
                "pandas": version("pandas"),
                "czsc": version("czsc"),
                "vectorbt": version("vectorbt"),
                "plotly": version("plotly"),
            },
        }
        _write_json(output_dir / "manifest.json", manifest)
        _write_text(
            output_dir / "report.md",
            render_backtest_report(data.symbol, baseline.version, metrics, chart_files),
        )
        return {
            "symbol": data.symbol,
            "baseline": baseline.version,
            "windows": windows,
            "output_dir": str(output_dir.resolve()),
        }
    except Exception as exc:
        _write_json(
            output_dir / "failure.json",
            {
                "status": "FAIL",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "run_at_utc": datetime.now(timezone.utc).isoformat(),
            },
        )
        raise
