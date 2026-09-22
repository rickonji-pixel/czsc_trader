"""PTE host adapter for strategy-owned SRT forward-observation charts."""

from __future__ import annotations

from pathlib import Path

from strategy_runtime import ChartRuntime, validate_chart_context


def render_forward_chart_html(context: object, *, strategy_root: Path) -> str:
    """Render the PTE observation exclusively through its SRT chart contract."""

    normalized = validate_chart_context(context, expected_mode="FORWARD_OBSERVATION")
    reference = normalized["strategy"]["reference_id"]
    return ChartRuntime(strategy_root).render_forward_observation(reference, normalized)
