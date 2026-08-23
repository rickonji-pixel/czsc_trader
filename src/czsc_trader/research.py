"""End-to-end reproducible research orchestration and evidence export."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone
from hashlib import sha256
from importlib.metadata import version
from itertools import count
import json
from pathlib import Path
import platform
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd

from .audit import audit_no_lookahead
from .backtest import PeriodBacktestResult, run_period_backtests
from .charting import DIVERGENCE_CONFIG, write_period_chart
from .data import SYMBOL, load_market_data
from .factors import generate_factor_frame
from .objectives import TARGET_PERIODS
from .walk_forward import CANDIDATES, Rule, select_fixed_rule


FEE_RATE = 0.0005
def create_output_dir(outputs_root: Path, symbol: str, run_date: date) -> Path:
    """Atomically create the next revision directory for a dated symbol run."""
    symbol_code = symbol.split(".", maxsplit=1)[0]
    outputs_root = Path(outputs_root)
    outputs_root.mkdir(parents=True, exist_ok=True)
    for revision in count(1):
        output_dir = outputs_root / f"{symbol_code}_{run_date:%m%d}_R{revision:02d}"
        try:
            output_dir.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return output_dir


def _atomic_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _atomic_csv(path: Path, frame: pd.DataFrame, *, index: bool = False) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=index, encoding="utf-8-sig")
    temporary.replace(path)


def _json_default(value: object) -> object:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _render_report(
    windows: dict[str, dict[str, float | int | bool | str]],
    audit: dict[str, int | str],
    unknown_values: dict[str, int],
    selected_rule: Rule,
    candidates: pd.DataFrame,
    chart_files: list[str],
) -> str:
    lines = [
        "# 588080 纯 CZSC 多因子固定策略研究结果",
        "",
        "## 目标区间",
        "",
        "| 区间 | 截止日 | 策略收益 | Buy & Hold | 超额收益 | 结果 |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for name, values in windows.items():
        lines.append(
            "| {name} | {end} | {strategy:.2%} | {buyhold:.2%} | {excess:.2%} | {status} |".format(
                name=name,
                end=values["end"],
                strategy=float(values["strategy_return"]),
                buyhold=float(values["buyhold_return"]),
                excess=float(values["excess_return"]),
                status="PASS" if values["pass"] else "FAIL",
            )
        )
    lines.extend(
        [
            "",
            "## 各周期独立指标",
            "",
            "| 区间 | 起始日 | 最大回撤 | 夏普比率 | 订单数 | 持仓比例 |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for name, values in windows.items():
        lines.append(
            "| {name} | {start} | {drawdown:.2%} | {sharpe:.3f} | {trades} | {exposure:.2%} |".format(
                name=name,
                start=values["start"],
                drawdown=float(values["max_drawdown"]),
                sharpe=float(values["sharpe"]),
                trades=int(values["trade_count"]),
                exposure=float(values["exposure"]),
            )
        )
    lines.extend(
        [
            "",
            "每个周期均以100万元现金、零持仓独立启动；首日开盘只执行上一交易日已经形成的信号。Buy & Hold 同样在首日开盘独立买入并计入手续费。",
            "",
            "## 交互式日线图",
            "",
            *[f"- [{name.removeprefix('chart_').removesuffix('.html')}]({name})" for name in chart_files],
            "",
            "## 无前视审计",
            "",
            f"- 状态：{audit['status']}",
            f"- 检查订单：{audit['orders_checked']}",
            f"- 检查订单来源事件：{audit['events_checked']}",
            f"- 检查每日仓位：{audit['positions_checked']}",
            "",
            "## 因子与参数说明",
            "",
            "因子全部来自 CZSC 1.0.1 的30分钟、日线和周线信号，固定规则在全历史保持不变，信号在下一交易日开盘执行。",
            "本结果允许查看2026年结果后选择固定候选，属于2026样本内优化，不是样本外验证。收益、基准、日期和组合净值均不能覆盖因子仓位。",
            f"候选规则：{len(candidates)}；选定权重：{selected_rule.weights}；入场/离场阈值：{selected_rule.enter}/{selected_rule.exit}；确认/最短持仓：{selected_rule.confirm_days}/{selected_rule.min_hold_days}。",
            f"未识别类别出现 {sum(unknown_values.values())} 次并按中性0分处理。",
            "",
            "## 限制",
            "",
            "数据仅覆盖单只ETF和约2.6年历史。结果用于验证这段样本上的因果策略流程，不证明跨标的或跨市场周期稳健性。",
            "",
        ]
    )
    return "\n".join(lines)


def _period_equity_output(
    result: PeriodBacktestResult,
    daily: pd.DataFrame,
    target_position: pd.Series,
    factor_score: pd.Series,
) -> pd.DataFrame:
    index = result.equity.index
    prices = daily.set_index("dt")
    execution_position = target_position.shift(1).loc[index]
    return pd.DataFrame(
        {
            "dt": index,
            "close": prices.loc[index, "close"].to_numpy(),
            "target_position": target_position.loc[index].to_numpy(),
            "execution_position": execution_position.to_numpy(),
            "factor_score": factor_score.loc[index].to_numpy(),
            "equity": result.equity.to_numpy(),
        }
    )


def run_research(raw_dir: Path, output_dir: Path) -> dict[str, object]:
    """Rebuild one fixed CZSC-only strategy, vectorbt results, and evidence."""
    raw_dir = Path(raw_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    data = load_market_data(raw_dir)
    factor_result = generate_factor_frame(data)
    selection = select_fixed_rule(data.daily, factor_result.frame, TARGET_PERIODS, fee_rate=FEE_RATE)
    factor_output = factor_result.frame.copy()
    factor_output.insert(0, "factor_score", selection.scores)
    factor_output.insert(0, "target_position", selection.target_position)
    factor_output.insert(2, "enter_threshold", float(selection.rule.enter))
    factor_output.insert(3, "exit_threshold", float(selection.rule.exit))
    factor_output.index.name = "dt"
    period_results = run_period_backtests(
        data.daily,
        selection.target_position,
        TARGET_PERIODS,
        fee_rate=FEE_RATE,
        factor_events=selection.events,
        factor_frame=factor_output,
    )
    combined_orders = pd.concat([result.orders for result in period_results.values()], ignore_index=True)
    event_pieces = [selection.events, *[result.factor_events for result in period_results.values()]]
    factor_events = pd.concat([piece for piece in event_pieces if not piece.empty], ignore_index=True)
    factor_events = factor_events.drop_duplicates(subset=["event_id"]).sort_values("signal_date").reset_index(drop=True)
    audit = audit_no_lookahead(combined_orders, factor_events, selection.target_position, factor_output)

    windows = {name: result.metrics for name, result in period_results.items()}
    metrics_payload = {
        "overall_pass": all(bool(values["pass"]) for values in windows.values()),
        "windows": windows,
        "audit": audit,
    }
    chart_files: list[str] = []
    for name, result in period_results.items():
        file_name = f"chart_{name}.html"
        write_period_chart(
            data.daily,
            factor_output,
            result.orders,
            pd.Timestamp(str(result.metrics["start"])),
            pd.Timestamp(str(result.metrics["end"])),
            f"588080 {name} · CZSC结构、因子信号与策略交易",
            output_dir / file_name,
        )
        chart_files.append(file_name)
    candidate_json = json.dumps([asdict(rule) for rule in CANDIDATES], sort_keys=True, ensure_ascii=False)
    selected_rule_payload = asdict(selection.rule)
    manifest = {
        "audit_status": audit["status"],
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_cutoff": str(data.daily["dt"].max().date()),
        "fee_rate_per_side": FEE_RATE,
        "period_initial_cash": 1_000_000.0,
        "period_start_policy": "independent cash portfolio; prior-day signal may execute at first open",
        "selection_mode": "fixed CZSC-only rule; 2026 sample-optimized",
        "position_policy": "target_position equals the selected CZSC factor state machine; no overlays",
        "selected_rule": selected_rule_payload,
        "charts": {
            "files": chart_files,
            "plotly": version("plotly"),
            "divergence_signals": [str(config["name"]) for config in DIVERGENCE_CONFIG],
            "data_policy": "warm-up allowed before period start; no bars after period end",
        },
        "raw_sha256": data.hashes,
        "candidate_space_sha256": sha256(candidate_json.encode("utf-8")).hexdigest(),
        "versions": {
            "python": platform.python_version(),
            "czsc": version("czsc"),
            "vectorbt": version("vectorbt"),
            "pandas": version("pandas"),
            "numpy": version("numpy"),
            "plotly": version("plotly"),
        },
    }

    _atomic_csv(output_dir / "factors.csv", factor_output, index=True)
    _atomic_csv(output_dir / "factor_events.csv", factor_events)
    _atomic_csv(output_dir / "candidate_results.csv", selection.candidates)
    _atomic_text(
        output_dir / "selected_rule.json",
        json.dumps(selected_rule_payload, ensure_ascii=False, indent=2),
    )
    for name, result in period_results.items():
        _atomic_csv(output_dir / f"orders_{name}.csv", result.orders)
        _atomic_csv(
            output_dir / f"equity_{name}.csv",
            _period_equity_output(
                result,
                data.daily,
                selection.target_position,
                selection.scores,
            ),
        )
    _atomic_text(
        output_dir / "metrics.json",
        json.dumps(metrics_payload, ensure_ascii=False, indent=2, default=_json_default),
    )
    _atomic_text(
        output_dir / "report.md",
        _render_report(
            windows,
            audit,
            factor_result.unknown_values,
            selection.rule,
            selection.candidates,
            chart_files,
        ),
    )
    _atomic_text(output_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return {
        "audit": audit,
        "windows": windows,
        "overall_pass": metrics_payload["overall_pass"],
        "selected_rule": selected_rule_payload,
        "output_dir": str(output_dir.resolve()),
    }


def run_dated_research(
    raw_dir: Path,
    outputs_root: Path,
    *,
    run_date: date | None = None,
    runner: Callable[[Path, Path], dict[str, object]] | None = None,
) -> dict[str, object]:
    """Run research in a new ``<symbol>_<MMDD>_RXX`` output directory."""
    effective_date = run_date or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    output_dir = create_output_dir(outputs_root, SYMBOL, effective_date)
    effective_runner = runner or run_research
    return effective_runner(Path(raw_dir), output_dir)
