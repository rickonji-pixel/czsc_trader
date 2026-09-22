from __future__ import annotations

from typing import Any

from .common import Guide, Panel, UnifiedStrategyCharts


class S002Charts(UnifiedStrategyCharts):
    def panels(self, context: dict[str, Any]) -> tuple[Panel, ...]:
        return (
            Panel(
                ("factor_score", "signal_active"),
                "事件状态",
                "#4fa5ff",
                (Guide("信号激活值", 1.0, "#ef4444"),),
                line_shape="hv",
            ),
        )
