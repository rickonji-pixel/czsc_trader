from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from strategy_runtime import (
    ExecutionState,
    PortfolioSnapshot,
    PublishedDataSource,
    StrategyInit,
    StrategyRelease,
    StrategyRuntime,
    TradableWindow,
    TradingPoint,
)


def test_legacy_runner_surface_is_not_public() -> None:
    import strategy_runtime

    for name in (
        "AccountSnapshot",
        "CalculationRequest",
        "DeploymentSpec",
        "ExecutionChannel",
        "StrategyDecision",
        "StrategyLoader",
        "StrategyRunner",
        "StrategyRuntimeContext",
        "load_strategy_runtime_context",
    ):
        assert not hasattr(strategy_runtime, name)


ROOT = Path(__file__).resolve().parents[4]
ZONE = ZoneInfo("Asia/Shanghai")


def _release(strategy_id: str, version: str) -> StrategyRelease:
    payload = json.loads(
        (ROOT / f"strategies/{strategy_id}/versions/{version}.json").read_text(
            encoding="utf-8"
        )
    )
    return StrategyRelease.from_mapping(payload)


def test_public_runtime_prepares_and_plans_without_an_execution_channel() -> None:
    trading_date = date(2026, 9, 21)
    strategy = StrategyRuntime().create(
        StrategyInit(
            _release("S007", "v1"),
            TradableWindow(trading_date, trading_date),
        )
    )
    prepared = strategy.prepare_data(PublishedDataSource(ROOT / "data/backtest"))
    calculated_at = datetime(2026, 9, 20, 22, 14, tzinfo=ZONE)
    plan = strategy.plan_at(
        data=prepared,
        point=TradingPoint(trading_date, calculated_at),
        portfolio=PortfolioSnapshot(
            "s007-v1",
            "588080.SH",
            Decimal("50000"),
            Decimal("100000"),
            5900,
            7,
            calculated_at,
        ),
        state=ExecutionState(3, calculated_at, 5900),
    )

    assert plan.strategy.reference_id == "S007-v1"
    assert plan.expected_portfolio_revision == 7
    assert plan.expected_state_revision == 3
    assert plan.actual_quantity == 5900
    assert plan.signal_identity != plan.plan_identity
    assert plan.trading_date == date(2026, 9, 21)
    assert {order.order_type.value for order in plan.orders} <= {"LIMIT", "MARKET"}
    assert {leg.order.order_type.value for leg in plan.legs} <= {"LIMIT", "MARKET"}
