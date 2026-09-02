"""Broker-neutral reconciliation and intervention state machine."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import secrets
from typing import Protocol

from .contracts import AdviceDecision, OrderSpec
from .store import PaperStore


class PaperTradingSafetyError(RuntimeError):
    pass


class PaperTradingStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrokerAccount:
    environment: str
    market: str
    cash: float
    total_assets: float
    frozen_cash: float


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    quantity: int


@dataclass(frozen=True)
class BrokerOrder:
    channel_order_id: str
    symbol: str
    side: str
    quantity: int
    limit_price: float
    status: str
    cumulative_filled_quantity: int
    average_fill_price: float
    remark: str


@dataclass(frozen=True)
class BrokerSnapshot:
    account: BrokerAccount
    positions: tuple[BrokerPosition, ...]
    orders: tuple[BrokerOrder, ...]
    quote_health: str


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    decision_id: str
    symbol: str
    side: str
    quantity: int
    limit_price: float
    order_type: str = "LIMIT"
    time_in_force: str = "DAY"


class AdviceClient(Protocol):
    def get_decision(self, actual_quantity: int) -> AdviceDecision: ...


class BrokerGateway(Protocol):
    def snapshot(self) -> BrokerSnapshot: ...

    def place_order(self, intent: OrderIntent) -> BrokerOrder: ...

    def cancel_order(self, channel_order_id: str) -> None: ...


class PaperTradingEngine:
    def __init__(
        self,
        store: PaperStore,
        broker: BrokerGateway,
        advice: AdviceClient,
        *,
        symbol: str,
    ) -> None:
        self.store = store
        self.broker = broker
        self.advice = advice
        self.symbol = symbol.upper()

    def _validate_snapshot(self, snapshot: BrokerSnapshot) -> None:
        if snapshot.account.environment != "SIMULATE":
            raise PaperTradingSafetyError("broker environment must be SIMULATE")
        if snapshot.account.market != "CN":
            raise PaperTradingSafetyError("broker market must be CN")
        for position in snapshot.positions:
            if position.quantity < 0 or position.quantity % 100:
                raise PaperTradingSafetyError("broker position must use non-negative 100-share lots")
        for order in snapshot.orders:
            if order.symbol != self.symbol:
                continue
            if order.quantity <= 0 or order.quantity % 100:
                raise PaperTradingSafetyError("broker order must use positive 100-share lots")

    def _reconcile_order(self, order: BrokerOrder) -> None:
        row = asdict(order)
        increment = self.store.upsert_order(row)
        if order.remark.startswith("PTE-") and self.store.get_intent(order.remark):
            self.store.bind_intent(order.remark, order.channel_order_id, order.status)
        if increment:
            self.store.add_event(
                "FILL_INCREMENT",
                {
                    "channel_order_id": order.channel_order_id,
                    "quantity": increment,
                    "cumulative_quantity": order.cumulative_filled_quantity,
                    "average_fill_price": order.average_fill_price,
                },
            )

    def refresh(self) -> dict[str, object]:
        snapshot = self.broker.snapshot()
        self._validate_snapshot(snapshot)
        for order in snapshot.orders:
            if order.symbol == self.symbol:
                self._reconcile_order(order)
        actual_quantity = sum(
            position.quantity for position in snapshot.positions if position.symbol == self.symbol
        )
        self.store.mark_reconciled()
        decision = self.advice.get_decision(actual_quantity)
        if decision.symbol != self.symbol:
            raise PaperTradingSafetyError("advice symbol differs from engine whitelist")
        if decision.actual_quantity != actual_quantity:
            raise PaperTradingSafetyError("advice actual quantity differs from broker reconciliation")
        if decision.order is not None and not self.store.is_paused():
            self._submit_once(decision, decision.order, snapshot)
        status = {
            "environment": snapshot.account.environment,
            "market": snapshot.account.market,
            "symbol": self.symbol,
            "quote_health": snapshot.quote_health,
            "paused": self.store.is_paused(),
            "account": asdict(snapshot.account),
            "actual_quantity": actual_quantity,
            "last_decision": asdict(decision),
            "orders": self.store.orders(),
            "alerts": [],
        }
        self.store.save_snapshot(status)
        return self.status()

    def _submit_once(
        self,
        decision: AdviceDecision,
        order: OrderSpec,
        snapshot: BrokerSnapshot,
    ) -> None:
        if self.store.find_intent_by_decision(decision.decision_id) is not None:
            return
        intent_id = f"PTE-{decision.decision_id}"
        matching = next((item for item in snapshot.orders if item.remark == intent_id), None)
        intent = OrderIntent(
            intent_id=intent_id,
            decision_id=decision.decision_id,
            symbol=decision.symbol,
            side=order.side,
            quantity=order.quantity,
            limit_price=order.limit_price,
        )
        self.store.save_intent(intent_id, decision.decision_id, asdict(intent))
        self.store.add_event("ORDER_INTENT_CREATED", asdict(intent))
        if matching is not None:
            self.store.bind_intent(intent_id, matching.channel_order_id, matching.status)
            return
        submitted = self.broker.place_order(intent)
        self._reconcile_order(submitted)
        self.store.bind_intent(intent_id, submitted.channel_order_id, submitted.status)
        self.store.add_event(
            "ORDER_SUBMITTED",
            {"intent_id": intent_id, "channel_order_id": submitted.channel_order_id},
        )

    def pause(self) -> dict[str, object]:
        self.store.set_paused(True)
        self.store.add_event("PAUSED", {})
        return self.status()

    def resume(self) -> dict[str, object]:
        if not self.store.has_reconciled():
            raise PaperTradingStateError("successful reconciliation required before resume")
        self.store.set_paused(False)
        self.store.add_event("RESUMED", {})
        return self.status()

    def issue_cancel_token(self, channel_order_id: str) -> str:
        if not any(order["channel_order_id"] == channel_order_id for order in self.store.orders()):
            raise PaperTradingStateError("unknown channel order")
        token = secrets.token_urlsafe(24)
        expires = datetime.now(timezone.utc) + timedelta(minutes=2)
        self.store.save_cancel_token(token, channel_order_id, expires.isoformat())
        self.store.add_event("CANCEL_TOKEN_ISSUED", {"channel_order_id": channel_order_id})
        return token

    def confirm_cancel(self, channel_order_id: str, token: str) -> dict[str, object]:
        now = datetime.now(timezone.utc).isoformat()
        result = self.store.consume_cancel_token(token, channel_order_id, now)
        if result == "bound":
            raise PaperTradingStateError("cancel token is bound to another order")
        if result != "ok":
            raise PaperTradingStateError("invalid or expired cancel token")
        self.broker.cancel_order(channel_order_id)
        self.store.add_event("CANCEL_REQUESTED", {"channel_order_id": channel_order_id})
        return self.status()

    def status(self) -> dict[str, object]:
        latest = self.store.latest_snapshot() or {
            "environment": "SIMULATE",
            "market": "CN",
            "symbol": self.symbol,
            "quote_health": "UNKNOWN",
            "account": None,
            "actual_quantity": None,
            "last_decision": None,
            "orders": self.store.orders(),
            "alerts": [],
        }
        latest["paused"] = self.store.is_paused()
        latest["events"] = self.store.recent_events(50)
        return latest
