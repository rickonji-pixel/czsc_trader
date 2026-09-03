"""Broker-neutral reconciliation and intervention state machine."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from functools import wraps
import secrets
from threading import RLock
from typing import Protocol

from .contracts import AdviceDecision, OrderSpec
from .channel_binding import load_channel_binding
from .store import PaperStore
from .trading_window import is_submission_window, shanghai_now


TERMINAL_ORDER_STATUSES = {
    "SUBMIT_FAILED",
    "FILLED_ALL",
    "CANCELLED_ALL",
    "FAILED",
    "DISABLED",
    "DELETED",
    "FILL_CANCELLED",
}


class PaperTradingSafetyError(RuntimeError):
    pass


class PaperTradingStateError(RuntimeError):
    pass


def synchronized(method):
    """Serialize scheduler and operator actions through one engine lock."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapped


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
    def get_decision(
        self, actual_quantity: int, available_cash: float,
        cycle_target_quantity: int | None = None, strategy_id: str | None = None,
        strategy_version: str | None = None, baseline: str | None = None,
    ) -> AdviceDecision: ...


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
        baseline: str = "baseline_20260903",
        strategy_id: str | None = None,
        strategy_version: str | None = None,
        release_hash: str | None = None,
        binding_required: bool = False,
        today: Callable[[], date] = date.today,
        now: Callable[[], datetime] = shanghai_now,
    ) -> None:
        self.store = store
        self.broker = broker
        self.advice = advice
        self.symbol = symbol.upper()
        self.baseline = baseline
        self.strategy_id = strategy_id
        self.strategy_version = strategy_version
        self.release_hash = release_hash
        self.binding_required = binding_required
        self.today = today
        self.now = now
        self._account_snapshot: BrokerSnapshot | None = None
        self._orders: tuple[BrokerOrder, ...] = ()
        self._decision: AdviceDecision | None = None
        self._decision_key: tuple[str | None, int, float, int | None] | None = None
        self._alerts: list[str] = []
        self._lock = RLock()

    def _validate_orders(self, orders: tuple[BrokerOrder, ...]) -> None:
        for order in orders:
            if order.symbol != self.symbol:
                continue
            if order.quantity <= 0 or order.quantity % 100:
                raise PaperTradingSafetyError("broker order must use positive 100-share lots")

    def _validate_snapshot(self, snapshot: BrokerSnapshot) -> None:
        if snapshot.account.environment != "SIMULATE":
            raise PaperTradingSafetyError("broker environment must be SIMULATE")
        if snapshot.account.market != "CN":
            raise PaperTradingSafetyError("broker market must be CN")
        for position in snapshot.positions:
            if position.quantity < 0 or position.quantity % 100:
                raise PaperTradingSafetyError("broker position must use non-negative 100-share lots")
        self._validate_orders(snapshot.orders)

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

    @synchronized
    def refresh(self) -> dict[str, object]:
        self.refresh_account()
        self.refresh_orders()
        return self.refresh_decision_if_changed(force=True)

    @synchronized
    def refresh_account(self) -> dict[str, object]:
        method = getattr(self.broker, "account_snapshot", None)
        snapshot = method() if method is not None else self.broker.snapshot()
        self._validate_snapshot(snapshot)
        self._account_snapshot = snapshot
        self.store.mark_reconciled()
        return self._save_status()

    @synchronized
    def refresh_orders(self) -> dict[str, object]:
        method = getattr(self.broker, "order_snapshot", None)
        if method is not None:
            orders = tuple(method())
        else:
            orders = tuple(self.broker.snapshot().orders)
        self._validate_orders(orders)
        for order in orders:
            if order.symbol == self.symbol:
                self._reconcile_order(order)
        self._orders = orders
        self._evaluate_submission()
        return self._save_status()

    @synchronized
    def refresh_decision_if_changed(self, *, force: bool = False) -> dict[str, object]:
        if self._account_snapshot is None:
            raise PaperTradingStateError("account reconciliation required before advice")
        if self.binding_required:
            binding = load_channel_binding(self.store)
            if binding is not None:
                new_identity = (binding.strategy_id, binding.strategy_version, binding.release_hash)
                old_identity = (self.strategy_id, self.strategy_version, self.release_hash)
                if new_identity != old_identity:
                    self.strategy_id, self.strategy_version, self.release_hash = new_identity
                    self._decision = None
                    self._decision_key = None
        if self.binding_required and self.strategy_id is None:
            raise PaperTradingStateError("Futu渠道尚未绑定策略，已阻止生成决策和新订单")
        actual_quantity = sum(
            position.quantity
            for position in self._account_snapshot.positions
            if position.symbol == self.symbol
        )
        identity_method = getattr(self.advice, "data_identity", None)
        identity = identity_method() if identity_method is not None else None
        available_cash = round(self._account_snapshot.account.cash, 2)
        saved_target = self.store.get_setting("cycle_target_quantity")
        cycle_target = int(saved_target) if saved_target else None
        key = (identity, actual_quantity, available_cash, cycle_target)
        if not force and self._decision is not None and key == self._decision_key:
            self._evaluate_submission()
            return self._save_status()
        advice_kwargs = {"cycle_target_quantity": cycle_target}
        if self.strategy_id is not None:
            advice_kwargs.update({
                "strategy_id": self.strategy_id,
                "strategy_version": self.strategy_version,
            })
        else:
            advice_kwargs["baseline"] = self.baseline
        decision = self.advice.get_decision(actual_quantity, available_cash, **advice_kwargs)
        if self.strategy_id is not None:
            actual_binding = (
                decision.strategy.get("strategy_id"), decision.strategy.get("version"),
                decision.strategy.get("release_hash"),
            )
            expected_binding = (self.strategy_id, self.strategy_version, self.release_hash)
            if actual_binding != expected_binding:
                raise PaperTradingSafetyError("策略决策身份与Futu渠道绑定不一致")
        if decision.symbol != self.symbol:
            raise PaperTradingSafetyError("advice symbol differs from engine whitelist")
        if decision.actual_quantity != actual_quantity:
            raise PaperTradingSafetyError("advice actual quantity differs from broker reconciliation")
        if round(decision.available_cash, 2) != available_cash:
            raise PaperTradingSafetyError("advice available cash differs from broker reconciliation")
        self._decision = decision
        if decision.cycle_target_quantity:
            self.store.set_setting("cycle_target_quantity", str(decision.cycle_target_quantity))
        elif actual_quantity == 0:
            self.store.set_setting("cycle_target_quantity", "")
        self._decision_key = key
        self._evaluate_submission()
        return self._save_status()

    def _evaluate_submission(self) -> None:
        decision = self._decision
        if decision is None:
            return
        active_orders = [
            order
            for order in self._orders
            if order.symbol == self.symbol and order.status not in TERMINAL_ORDER_STATUSES
        ]
        alerts: list[str] = []
        if decision.orders and not self.store.is_paused():
            if decision.valid_session != self.today():
                alerts.append("DECISION_NOT_VALID_TODAY")
            elif not is_submission_window(self.now()):
                alerts.append("OUTSIDE_SUBMISSION_WINDOW")
            elif active_orders:
                alerts.append("ACTIVE_ORDER_BLOCKS_SUBMISSION")
            else:
                assert self._account_snapshot is not None
                snapshot = BrokerSnapshot(
                    self._account_snapshot.account,
                    self._account_snapshot.positions,
                    self._orders,
                    self._account_snapshot.quote_health,
                )
                for index, order in enumerate(decision.orders):
                    decision_key = (
                        decision.decision_id if len(decision.orders) == 1
                        else f"{decision.decision_id}:{index}"
                    )
                    existing = self.store.find_intent_by_decision(decision_key)
                    if existing is None or existing["status"] == "PENDING_SUBMIT":
                        self._submit_once(decision, order, snapshot, index=index, decision_key=decision_key)
                        break
        self._alerts = alerts

    def _save_status(self) -> dict[str, object]:
        if self._account_snapshot is None:
            return self.status()
        snapshot = self._account_snapshot
        actual_quantity = sum(
            position.quantity for position in snapshot.positions if position.symbol == self.symbol
        )
        status = {
            "environment": snapshot.account.environment,
            "market": snapshot.account.market,
            "symbol": self.symbol,
            "quote_health": snapshot.quote_health,
            "paused": self.store.is_paused(),
            "account": asdict(snapshot.account),
            "actual_quantity": actual_quantity,
            "last_decision": None if self._decision is None else asdict(self._decision),
            "orders": self.store.orders(),
            "alerts": self._alerts,
        }
        self.store.save_snapshot(status)
        return self.status()

    def _submit_once(
        self,
        decision: AdviceDecision,
        order: OrderSpec,
        snapshot: BrokerSnapshot,
        *,
        index: int = 0,
        decision_key: str | None = None,
    ) -> None:
        decision_key = decision_key or decision.decision_id
        intent_id = f"PTE-{decision.decision_id}" if len(decision.orders) == 1 else f"PTE-{decision.decision_id}-{index}"
        existing = self.store.find_intent_by_decision(decision_key)
        matching = next((item for item in snapshot.orders if item.remark == intent_id), None)
        intent = OrderIntent(
            intent_id=intent_id,
            decision_id=decision_key,
            symbol=decision.symbol,
            side=order.side,
            quantity=order.quantity,
            limit_price=order.limit_price,
        )
        if matching is not None:
            self.store.bind_intent(intent_id, matching.channel_order_id, matching.status)
            return
        if existing is not None:
            if existing["channel_order_id"] is not None or existing["status"] != "PENDING_SUBMIT":
                return
            self.store.add_event("ORDER_INTENT_RECOVERED", asdict(intent))
        else:
            self.store.save_intent(intent_id, decision_key, asdict(intent))
            self.store.add_event("ORDER_INTENT_CREATED", asdict(intent))
        submitted = self.broker.place_order(intent)
        self._reconcile_order(submitted)
        self._orders = tuple(
            item for item in self._orders if item.channel_order_id != submitted.channel_order_id
        ) + (submitted,)
        self.store.bind_intent(intent_id, submitted.channel_order_id, submitted.status)
        self.store.add_event(
            "ORDER_SUBMITTED",
            {"intent_id": intent_id, "channel_order_id": submitted.channel_order_id},
        )

    @synchronized
    def pause(self) -> dict[str, object]:
        self.store.set_paused(True)
        self.store.add_event("PAUSED", {})
        return self.status()

    @synchronized
    def resume(self) -> dict[str, object]:
        if not self.store.has_reconciled():
            raise PaperTradingStateError("successful reconciliation required before resume")
        self.store.set_paused(False)
        self.store.add_event("RESUMED", {})
        return self.status()

    @synchronized
    def issue_cancel_token(self, channel_order_id: str) -> str:
        if not any(order["channel_order_id"] == channel_order_id for order in self.store.orders()):
            raise PaperTradingStateError("unknown channel order")
        token = secrets.token_urlsafe(24)
        expires = datetime.now(timezone.utc) + timedelta(minutes=2)
        self.store.save_cancel_token(token, channel_order_id, expires.isoformat())
        self.store.add_event("CANCEL_TOKEN_ISSUED", {"channel_order_id": channel_order_id})
        return token

    @synchronized
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

    @synchronized
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
        alerts = list(latest.get("alerts", []))
        if (
            self.store.get_setting("data_publication_error")
            and "DATA_PUBLICATION_FAILED" not in alerts
        ):
            alerts.append("DATA_PUBLICATION_FAILED")
        latest["alerts"] = alerts
        latest["events"] = self.store.recent_events(50)
        latest["scheduler_failures"] = self.store.operation_failures()
        return latest

    @synchronized
    def close(self) -> None:
        try:
            close_broker = getattr(self.broker, "close", None)
            if close_broker is not None:
                close_broker()
        finally:
            self.store.close()
