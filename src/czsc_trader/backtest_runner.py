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
from .baselines import resolve_baseline
from .charting import write_period_chart
from .data import load_market_data
from .factors import generate_factor_frame
from .output_paths import create_output_dir


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


def _comparison_metrics(
    daily: pd.DataFrame,
    result: PeriodBacktestResult,
    *,
    fee_rate: float,
    init_cash: float,
) -> dict[str, float | str]:
    """Project ordinary-backtest output without changing research metrics."""
    start = pd.Timestamp(str(result.metrics["start"]))
    end = pd.Timestamp(str(result.metrics["end"]))
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
    strategy_return = float(result.metrics["strategy_return"])
    buyhold_return = float(result.metrics["buyhold_return"])
    strategy_sharpe = float(result.metrics["sharpe"])
    buyhold_sharpe = float(buyhold.metrics["sharpe"])
    strategy_max_drawdown = float(result.metrics["max_drawdown"])
    buyhold_max_drawdown = float(buyhold.metrics["max_drawdown"])
    return {
        "start": str(result.metrics["start"]),
        "end": str(result.metrics["end"]),
        "strategy_return": strategy_return,
        "buyhold_return": buyhold_return,
        "return_difference": strategy_return - buyhold_return,
        "strategy_sharpe": strategy_sharpe,
        "buyhold_sharpe": buyhold_sharpe,
        "sharpe_difference": strategy_sharpe - buyhold_sharpe,
        "strategy_max_drawdown": strategy_max_drawdown,
        "buyhold_max_drawdown": buyhold_max_drawdown,
        "max_drawdown_difference": strategy_max_drawdown - buyhold_max_drawdown,
    }


def _report(
    symbol: str,
    baseline_version: str,
    metrics: dict[str, object],
    chart_files: list[str],
) -> str:
    lines = [
        f"# {symbol} 固定基线规则回测",
        "",
        f"- 规则基线：`{baseline_version}`",
        "- 本次只应用冻结规则，未执行候选搜索或参数选优。",
        "",
        "## 策略与 Buy & Hold 比较",
        "",
        "| 区间 | 开始 | 结束 | 策略收益 | Buy & Hold收益 | 收益差 | 策略夏普 | Buy & Hold夏普 | 夏普差 | 策略最大回撤 | Buy & Hold最大回撤 | 最大回撤差 |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    windows = metrics["windows"]
    assert isinstance(windows, dict)
    for name, values in windows.items():
        assert isinstance(values, dict)
        lines.append(
            f"| {name} | {values['start']} | {values['end']} | "
            f"{float(values['strategy_return']):.2%} | {float(values['buyhold_return']):.2%} | "
            f"{float(values['return_difference']):.2%} | "
            f"{float(values['strategy_sharpe']):.3f} | "
            f"{float(values['buyhold_sharpe']):.3f} | "
            f"{float(values['sharpe_difference']):.3f} | "
            f"{float(values['strategy_max_drawdown']):.2%} | "
            f"{float(values['buyhold_max_drawdown']):.2%} | "
            f"{float(values['max_drawdown_difference']):.2%} |"
        )
    lines.extend(["", "## 交互式图表", ""])
    lines.extend(f"- [{name}]({name})" for name in chart_files)
    return "\n".join(lines) + "\n"


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
            name: _comparison_metrics(
                causal_data.daily,
                result,
                fee_rate=request.fee_rate,
                init_cash=request.init_cash,
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

        _write_csv(output_dir / "factors.csv", factor_output, index=True)
        _write_csv(output_dir / "factor_events.csv", factor_events)
        _write_json(output_dir / "audit.json", audit)
        _write_json(output_dir / "metrics.json", metrics)
        _write_json(output_dir / "baseline_rule.json", baseline.rule_payload)
        manifest = {
            "run_type": "fixed_baseline_backtest",
            "run_at_utc": datetime.now(timezone.utc).isoformat(),
            "symbol": data.symbol,
            "asset_type": data.asset_type,
            "fee_rate_per_side": request.fee_rate,
            "initial_cash": request.init_cash,
            "baseline": {
                "version": baseline.version,
                "sha256": baseline.sha256,
                "verification_snapshot": baseline.verification_snapshot,
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
        _write_text(output_dir / "report.md", _report(data.symbol, baseline.version, metrics, chart_files))
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
