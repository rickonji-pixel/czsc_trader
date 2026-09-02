"""Pure Markdown rendering for fixed-baseline backtest results."""

from __future__ import annotations


def render_backtest_report(
    symbol: str,
    baseline_version: str,
    metrics: dict[str, object],
    chart_files: list[str],
) -> str:
    """Render one backtest report without reading or writing files."""

    def percent(value: object) -> str:
        return "N/A" if value is None else f"{float(value):.2%}"

    def ratio(value: object) -> str:
        return "N/A" if value is None else f"{float(value):.3f}"

    def win_loss(item: dict[str, object]) -> str:
        status = item.get("win_loss_ratio_status")
        labels = {
            "NO_LOSSES": "无亏损",
            "NO_WINS": "无盈利",
            "NO_CLOSED_TRADES": "无闭合交易",
        }
        if status == "VALID":
            return ratio(item.get("win_loss_ratio"))
        return labels.get(str(status), "N/A")

    lines = [
        f"# {symbol} 固定基线规则回测",
        "",
        f"- 规则基线：`{baseline_version}`",
        "- 本次只应用冻结规则，未执行候选搜索或参数选优。",
        "",
        "## 策略比较",
    ]
    windows = metrics["windows"]
    if not isinstance(windows, dict):
        raise TypeError("metrics windows must be a mapping")
    labels = {
        "active_baseline": "活动基线·次日开盘",
        "active_baseline_execution_policy": "活动基线·执行规则",
        "buyhold": "BuyHold",
        "ma5_ma20": "MA5/MA20",
    }
    for name, values in windows.items():
        if not isinstance(values, dict):
            raise TypeError("window metrics must be a mapping")
        lines.extend(
            [
                "",
                f"## {name}",
                "",
                f"区间：{values['start']} 至 {values['end']}",
                "",
                "| 策略 | 最大回撤 | 卡玛比率 | 盈亏比 | 收益率 | 夏普率 |",
                "| --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        strategies = values["strategies"]
        if not isinstance(strategies, dict):
            raise TypeError("window strategies must be a mapping")
        for strategy_id in (
            "active_baseline",
            "active_baseline_execution_policy",
            "buyhold",
            "ma5_ma20",
        ):
            if strategy_id not in strategies:
                continue
            item = strategies[strategy_id]
            if not isinstance(item, dict):
                raise TypeError("strategy metrics must be a mapping")
            lines.append(
                f"| {labels[strategy_id]} | {percent(item['max_drawdown'])} | "
                f"{ratio(item['calmar'])} | {win_loss(item)} | "
                f"{percent(item['return'])} | {ratio(item['sharpe'])} |"
            )
    lines.extend(["", "## 交互式图表", ""])
    lines.extend(f"- [{name}]({name})" for name in chart_files)
    return "\n".join(lines) + "\n"
