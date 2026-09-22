from __future__ import annotations

from typing import Any

from .common import Guide, Panel, UnifiedStrategyCharts


class S001Charts(UnifiedStrategyCharts):
    def panels(self, context: dict[str, Any]) -> tuple[Panel, ...]:
        rule = context["strategy"]["configuration"]["rule"]
        return (
            Panel(
                ("factor_score",),
                "策略得分",
                "#4fa5ff",
                (
                    Guide("买入阈值", float(rule["entry_threshold"]), "#ef4444"),
                    Guide("卖出阈值", float(rule["exit_threshold"]), "#22c55e"),
                ),
            ),
        )
