from decimal import Decimal
from types import SimpleNamespace

import pytest
from longbridge.openapi import OrderSide, OrderStatus, OrderType, OutsideRTH

from live_execution_gateway.longbridge_readonly import (
    LongbridgeReadOnlyGateway,
    _order,
    require_us_stock,
)


class FakeTradeContext:
    def __init__(self):
        self.calls = []

    def account_balance(self, currency=None):
        self.calls.append(("account_balance", currency))
        cash = SimpleNamespace(
            currency="USD", available_cash=Decimal("1000.50"),
            withdraw_cash=Decimal("900"), frozen_cash=Decimal("0"),
            settling_cash=Decimal("100.50"),
        )
        return [SimpleNamespace(
            currency="USD", total_cash=Decimal("1000.50"),
            net_assets=Decimal("1000.50"), buy_power=Decimal("1000.50"),
            risk_level=0, margin_call=Decimal("0"), init_margin=Decimal("0"),
            maintenance_margin=Decimal("0"), max_finance_amount=Decimal("0"),
            remaining_finance_amount=Decimal("0"), cash_infos=[cash],
        )]

    def stock_positions(self, symbols=None):
        self.calls.append(("stock_positions", symbols))
        position = SimpleNamespace(
            symbol="MU.US", symbol_name="Micron", quantity=Decimal("2"),
            available_quantity=Decimal("2"), currency="USD",
            cost_price=Decimal("100"), market="US", init_quantity=Decimal("2"),
        )
        return SimpleNamespace(channels=[SimpleNamespace(positions=[position])])

    def today_orders(self, symbol=None):
        self.calls.append(("today_orders", symbol))
        return []

    def today_executions(self, symbol=None):
        self.calls.append(("today_executions", symbol))
        return []

    def history_orders(self, symbol=None, start_at=None, end_at=None):
        self.calls.append(("history_orders", symbol, start_at, end_at))
        return []

    def history_executions(self, symbol=None, start_at=None, end_at=None):
        self.calls.append(("history_executions", symbol, start_at, end_at))
        return []

    def submit_order(self, **kwargs):  # pragma: no cover
        raise AssertionError("read-only gateway must never call submit_order")


def test_gateway_exposes_only_read_capabilities() -> None:
    context = FakeTradeContext()
    gateway = LongbridgeReadOnlyGateway(symbol="mu.us", trade_context=context)

    snapshot = gateway.snapshot()
    history = gateway.history(days=30)

    assert snapshot["order_write_capability"] is False
    assert snapshot["positions"][0]["quantity"] == "2"
    assert history["order_write_capability"] is False
    assert not hasattr(gateway, "submit_order")
    assert not hasattr(gateway, "replace_order")
    assert not hasattr(gateway, "cancel_order")
    assert gateway.capability_report()["symbol_allowlist"] == ["MU.US"]
    assert [item[0] for item in context.calls] == [
        "account_balance", "stock_positions", "today_orders", "today_executions",
        "history_orders", "history_executions",
    ]


def test_gateway_rejects_non_us_stock_scope() -> None:
    with pytest.raises(ValueError, match="US stocks only"):
        require_us_stock("00700.HK")
    with pytest.raises(ValueError, match="between 1 and 90"):
        LongbridgeReadOnlyGateway(
            symbol="MU.US", trade_context=FakeTradeContext(),
        ).history(days=365)


def test_installed_sdk_enums_are_normalized_before_reconciliation() -> None:
    normalized = _order(SimpleNamespace(
        order_id="LB-1", status=OrderStatus.New, side=OrderSide.Buy,
        order_type=OrderType.LO, outside_rth=OutsideRTH.AnyTime,
    ))

    assert normalized["status"] == "New"
    assert normalized["side"] == "Buy"
    assert normalized["order_type"] == "LO"
    assert normalized["outside_rth"] == "AnyTime"
