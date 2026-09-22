from __future__ import annotations

from typing import Any

from .common import Guide, Panel, UnifiedStrategyCharts


class S007Charts(UnifiedStrategyCharts):
    def panels(self, context: dict[str, Any]) -> tuple[Panel, ...]:
        score = context["strategy"]["configuration"]["rule"]["score"]
        return (
            Panel(
                ("factor_score", "base_score"),
                "基础分",
                "#4fa5ff",
                (
                    Guide("入场阈值", float(score["entry_threshold"]), "#ef4444"),
                    Guide("退出阈值", float(score["exit_threshold"]), "#22c55e"),
                ),
            ),
            Panel(
                ("confirmation_score",),
                "确认分",
                "#c586ff",
                (
                    Guide(
                        "确认门",
                        float(score["confirmation_threshold"]),
                        "#c586ff",
                    ),
                ),
            ),
        )
