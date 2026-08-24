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
from .backtest import PeriodBacktestResult, run_period_backtests
from .baselines import resolve_baseline
from .charting import write_period_chart
from .data import load_market_data
from .factors import generate_factor_frame
from .output_paths import create_output_dir
from .rules import apply_fixed_rule


@dataclass(frozen=True)
class BacktestRequest:
    symbol: str
    asset_type: str
    start: date | None = None
    end: date | None = None
    baseline: str | None = None
    targets_path: Path | None = None
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


def _load_target_config(path: Path) -> tuple[
    dict[str, tuple[pd.Timestamp, pd.Timestamp]], dict[str, float]
]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read target config {path}: {exc}") from exc
    periods_payload = payload.get("periods") if isinstance(payload, dict) else None
    if not isinstance(periods_payload, dict) or not periods_payload:
        raise ValueError("target config periods must be a non-empty object")
    periods: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {}
    targets: dict[str, float] = {}
    for name, item in periods_payload.items():
        if not isinstance(item, dict):
            raise ValueError(f"target period {name} must be an object")
        try:
            start = pd.Timestamp(str(item["start"]))
            end = pd.Timestamp(str(item["end"]))
            target = float(item["min_return"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"target period {name} requires start, end and min_return") from exc
        if start > end:
            raise ValueError(f"target period {name} starts after it ends")
        periods[str(name)] = (start, end)
        targets[str(name)] = target
    return periods, targets


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
        f"- 验收状态：`{metrics['acceptance_status']}`",
        "- 本次只应用冻结规则，未执行候选搜索或参数选优。",
        "",
        "## 区间指标",
        "",
        "| 区间 | 开始 | 结束 | 策略收益 | 最大回撤 | 夏普率 | 交易次数 | 状态 |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    windows = metrics["windows"]
    assert isinstance(windows, dict)
    for name, values in windows.items():
        assert isinstance(values, dict)
        lines.append(
            f"| {name} | {values['start']} | {values['end']} | "
            f"{float(values['strategy_return']):.2%} | {float(values['max_drawdown']):.2%} | "
            f"{float(values['sharpe']):.3f} | {int(values['trade_count'])} | {values['status']} |"
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
    if request.targets_path is not None and (request.start is not None or request.end is not None):
        raise ValueError("--targets cannot be combined with --start or --end")
    effective_date = run_date or datetime.now().astimezone().date()
    output_dir = create_output_dir(request.outputs_root, request.symbol, effective_date)
    try:
        baseline = resolve_baseline(request.baseline_root, request.baseline)
        data = load_market_data(request.raw_dir, request.symbol, request.asset_type)
        if request.targets_path is not None:
            periods, return_targets = _load_target_config(request.targets_path)
            cutoff = max(end for _, end in periods.values())
        else:
            start = pd.Timestamp(request.start or data.daily["dt"].min().date())
            end = pd.Timestamp(request.end or data.daily["dt"].max().date())
            if start > end:
                raise ValueError("backtest start must not be after end")
            periods = {"full": (start, end)}
            return_targets = None
            cutoff = end
        causal_data = data.truncate(cutoff)
        factor_result = generate_factor_frame(causal_data)
        applied = apply_fixed_rule(factor_result.frame, baseline.rule)
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
            return_targets=return_targets,
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
        windows = {name: result.metrics for name, result in results.items()}
        acceptance_status = (
            "N/A"
            if return_targets is None
            else "PASS" if all(bool(values["pass"]) for values in windows.values()) else "FAIL"
        )
        metrics: dict[str, object] = {
            "acceptance_status": acceptance_status,
            "windows": windows,
            "audit": audit,
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
            "acceptance_status": acceptance_status,
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
            "acceptance_status": acceptance_status,
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
