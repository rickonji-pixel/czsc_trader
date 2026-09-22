from __future__ import annotations

from typing import Any

from .common import Panel, UnifiedStrategyCharts


class S003Charts(UnifiedStrategyCharts):
    def panels(self, context: dict[str, Any]) -> tuple[Panel, ...]:
        return (
            Panel(
                ("factor_score", "moneyflow_breadth"),
                "资金流宽度",
                "#4fa5ff",
                dynamic_threshold="threshold",
            ),
        )
