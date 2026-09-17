from __future__ import annotations

from dataclasses import replace
from datetime import date

from paper_trading_engine.contracts import AdviceDecision, OrderSpec
from paper_trading_engine.broker import (
    BrokerAccount,
    BrokerOrder,
    BrokerSnapshot,
)


def decision(order: OrderSpec | None = None) -> AdviceDecision:
    orders = () if order is None else (order,)
    target = 0 if order is None else order.quantity
    return AdviceDecision(
        contract_version="advice.v4",
        decision_id="DEC-ONE",
        symbol="588080.SH",
        signal_date=date(2026, 9, 1),
        valid_session=date(2026, 9, 2),
        actual_quantity=0,
        target_quantity=target,
        cycle_target_quantity=target,
        delta_quantity=target,
        action="WAIT" if order is None else order.side,
        strategy={
            "strategy_id": "S001",
            "name": "综合基线策略",
            "version": "v1",
            "release_id": "S001-v1",
            "release_hash": "b" * 64,
            "qualification": "PAPER_READY",
        },
        signal_reference_price=1.7,
        execution_reference_price=1.68,
        data_cutoff=date(2026, 9, 1),
        order=order,
        orders=orders,
        available_cash=1_000_000,
        fee_rate=0.0005,
    )


def broker_snapshot(*, orders=(), quantity=0, symbol="588080.SH") -> BrokerSnapshot:
    from paper_trading_engine.broker import BrokerPosition

    positions = () if quantity == 0 else (BrokerPosition(symbol, quantity),)
    return BrokerSnapshot(
        BrokerAccount("SIMULATE", "CN", 1_000_000, 1_000_000, 0),
        positions,
        tuple(orders),
    )


class FakeAdvice:
    def __init__(self, value: AdviceDecision):
        self.value = value
        self.calls = []

    def data_identity(self):
        return "2026-09-01"

    def get_decision(self, actual_quantity, available_cash, **kwargs):
        self.calls.append((actual_quantity, available_cash, kwargs))
        value = replace(
            self.value,
            actual_quantity=actual_quantity,
            available_cash=available_cash,
            source_decision_id=self.value.source_decision_id or self.value.decision_id,
        )
        transform = kwargs.get("decision_transform")
        return transform(value) if transform else value


class FakeBroker:
    channel_id = "futu"

    def __init__(self):
        self.value = broker_snapshot()
        self.placed = []
        self.cancelled = []

    def snapshot(self):
        return self.value

    def account_snapshot(self):
        return self.value

    def order_snapshot(self):
        return self.value.orders

    def historical_order_snapshot(self, start, end):
        return self.value.orders

    def place_order(self, intent):
        self.placed.append(intent)
        order = BrokerOrder(
            str(1000 + len(self.placed)), intent.symbol, intent.side, intent.quantity,
            intent.limit_price, "SUBMITTED", 0, 0, intent.intent_id,
            order_type=intent.order_type,
        )
        self.value = replace(self.value, orders=(*self.value.orders, order))
        return order

    def cancel_order(self, channel_order_id):
        self.cancelled.append(channel_order_id)

    def close(self):
        return None
