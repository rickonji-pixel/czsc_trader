from __future__ import annotations

from .models import StrategySnapshot


def render_report(snapshot: StrategySnapshot, metrics: dict[str, object]) -> str:
    def value(name: str) -> str:
        item = metrics[name]
        return "不可用" if item is None else f"{float(item):.4f}"

    return (
        f"# {snapshot.identity.reference} 回测报告\n\n"
        "本报告由 TDR Backtest v2 基于确定性账户回放生成。成交均为虚拟成交。\n\n"
        "| 最大回撤 | 卡玛比率 | 盈亏比 | 收益率 | 夏普率 | 闭合交易 |\n"
        "| ---: | ---: | ---: | ---: | ---: | ---: |\n"
        f"| {value('max_drawdown')} | {value('calmar')} | "
        f"{value('win_loss_ratio')} | {value('return')} | {value('sharpe')} | "
        f"{metrics['closed_trades']} |\n"
    )
