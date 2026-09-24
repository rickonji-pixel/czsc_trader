from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from strategy_runtime import (
    CutoffRule,
    DecisionContract,
    ExecutionPolicy,
    ImplementationRef,
    InputContract,
    InputRequirement,
    MonitoringPolicy,
    OrderSide,
    OrderType,
    ParameterSet,
    PlannedOrder,
    PortfolioSnapshot,
    ExecutionState,
    RequiredCapabilities,
    RuntimeDefinition,
    RuntimeContractError,
    StrategyImplementation,
)


RELEASE_HASH = "a" * 64


def test_us_stock_uses_whole_shares_without_changing_a_share_lots() -> None:
    now = datetime(2026, 9, 22, tzinfo=timezone.utc)
    portfolio = PortfolioSnapshot(
        "us-account", "MU.US", Decimal("1000"), Decimal("2000"), 37, 0, now,
    )
    order = PlannedOrder(OrderSide.BUY, 37, OrderType.MARKET, Decimal("100"), lot_size=1)
    state = ExecutionState(0, now, cycle_target_quantity=37, lot_size=1)
    assert portfolio.position_quantity == order.quantity == state.cycle_target_quantity
    with pytest.raises(RuntimeContractError, match="100-share lots"):
        PortfolioSnapshot(
            "cn-account", "588080.SH", Decimal("1000"), Decimal("2000"), 37, 0, now,
        )


def test_strategy_implementation_requires_every_strategy_owned_method() -> None:
    class IncompleteStrategy(StrategyImplementation):
        pass

    with pytest.raises(TypeError, match="abstract methods.*calculate_history"):
        IncompleteStrategy()


def _requirement() -> InputRequirement:
    return InputRequirement(
        name="daily_bars",
        dataset="etf.ohlcv",
        subject="588080.SH",
        frequency="daily",
        lookback_sessions=252,
        cutoff_rule=CutoffRule.SIGNAL_SESSION,
    )


def _definition() -> RuntimeDefinition:
    return RuntimeDefinition(
        schema_version=1,
        strategy_family_id="S007",
        version="v1",
        release_id="S007-v1",
        release_hash=RELEASE_HASH,
        implementation=ImplementationRef(
            "strategy_runtime.strategies.s007_v1",
            "S007V1",
            1,
            "b" * 64,
        ),
        parameters=ParameterSet({"entry_threshold": 0.1}),
        inputs=InputContract((_requirement(),)),
        decision=DecisionContract("TARGET_POSITION", 0.0, 1.0, "NEXT_SESSION"),
        execution=ExecutionPolicy("MARKETABLE_LIMIT", {"limit_ratio": 0.2}),
        monitoring=MonitoringPolicy("ROLLING", {"window_sessions": 60}),
        capabilities=RequiredCapabilities(("etf.ohlcv",), ("LIMIT",)),
    )


def test_runtime_definition_is_family_version_scoped_and_immutable() -> None:
    definition = _definition()

    assert definition.strategy_family_id == "S007"
    assert definition.release_id == "S007-v1"
    assert definition.parameters.sha256
    with pytest.raises(TypeError):
        definition.parameters.values["entry_threshold"] = 0.2

    nested = ParameterSet({"weights": {"price": 0.5}, "windows": [20, 60]})
    with pytest.raises(TypeError):
        nested.values["weights"]["price"] = 0.7
    assert nested.values["windows"] == (20, 60)


def test_runtime_definition_requires_input_capabilities() -> None:
    with pytest.raises(RuntimeContractError, match="input datasets"):
        RuntimeDefinition(
            schema_version=1,
            strategy_family_id="S007",
            version="v1",
            release_id="S007-v1",
            release_hash=RELEASE_HASH,
            implementation=ImplementationRef("runtime", "Strategy", 1, "b" * 64),
            parameters=ParameterSet({}),
            inputs=InputContract((_requirement(),)),
            decision=DecisionContract("TARGET_POSITION", 0.0, 1.0, "NEXT_SESSION"),
            execution=ExecutionPolicy("LIMIT", {}),
            monitoring=MonitoringPolicy("ROLLING", {}),
            capabilities=RequiredCapabilities(("macro.shibor_daily",), ("LIMIT",)),
        )
