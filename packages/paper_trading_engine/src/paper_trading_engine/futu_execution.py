"""Shared Futu execution and reconciliation for account-owned orders."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import secrets

from .audit import AuditRecorder
from .broker import OrderIntent, PaperTradingSafetyError, TERMINAL_ORDER_STATUSES


class ChannelReconciliationError(RuntimeError):
    pass


class FutuExecution:
    def __init__(self, store, broker, *, symbol: str, today=date.today, audit=None) -> None:
        self.store = store
        self.broker = broker
        self.symbol = symbol.upper()
        self.today = today
        self.audit = audit or AuditRecorder(store)
        self._snapshot = None
        self._orders = ()
        self._draining = False

    def begin_shutdown(self) -> None:
        self._draining = True

    def refresh_account(self):
        method = getattr(self.broker, "account_snapshot", None)
        snapshot = method() if method is not None else self.broker.snapshot()
        if snapshot.account.environment != "SIMULATE":
            raise PaperTradingSafetyError("broker environment must be SIMULATE")
        if snapshot.account.market != "CN":
            raise PaperTradingSafetyError("broker market must be CN")
        allocated = sum(float(row["initial_cash"]) for row in self.store.virtual_accounts())
        if allocated > float(snapshot.account.total_assets) + 0.01:
            raise ChannelReconciliationError("虚拟账户分配资金超过Futu总资产")
        self._snapshot = snapshot
        self.store.mark_reconciled()
        return self.status()

    def _owned_intent(self, remark: str):
        return self.store.account_intent(remark) if remark.startswith("PTE-") else None

    def refresh_orders(self):
        previous_reconciliation = self.store.get_setting("channel_reconciliation_status")
        orders = tuple(self.broker.order_snapshot())
        for order in orders:
            intent = self._owned_intent(order.remark)
            if intent is None:
                if order.status not in TERMINAL_ORDER_STATUSES:
                    self.store.set_setting("channel_reconciliation_status", "BLOCKED")
                    self.audit.record(
                        "CHANNEL_RECONCILIATION_FAILED", source="futu_execution",
                        outcome="FAILURE", channel="futu", order_id=order.channel_order_id,
                        details={"reason": "unowned_order", "remark": order.remark},
                    )
                    raise ChannelReconciliationError(
                        f"Futu订单无法归属虚拟账户: {order.channel_order_id}"
                    )
                continue
            if order.symbol != intent["symbol"]:
                self.store.set_setting("channel_reconciliation_status", "BLOCKED")
                raise ChannelReconciliationError(
                    f"Futu订单标的与本地意图不一致: {order.channel_order_id}"
                )
            previous_status = None
            try:
                previous_status = self.store.account_order(order.channel_order_id).get("status")
            except KeyError:
                account = self.store.virtual_account(intent["account_id"])
                recovered = self.audit.build(
                    "ORDER_INTENT_RECOVERED", source="futu_execution",
                    account_id=intent["account_id"], strategy_id=account["strategy_id"],
                    strategy_version=account["strategy_version"],
                    release_hash=account["release_hash"], channel="futu",
                    decision_id=intent["decision_id"], order_id=order.channel_order_id,
                    correlation_id=intent["decision_id"],
                    details={"intent_id": intent["intent_id"], "status": order.status},
                )
                self.store.bind_channel_order(
                    intent["intent_id"], order.channel_order_id, asdict(order), recovered,
                )
                self.store.set_virtual_health(intent["account_id"], "OK")
            fill = self.store.apply_fill_increment(
                order.channel_order_id,
                cumulative_quantity=order.cumulative_filled_quantity,
                average_price=order.average_fill_price,
                occurred_at=self.store.get_setting("clock_override")
                or datetime.now(timezone.utc).isoformat(),
            )
            if fill is not None:
                event_type = (
                    "ORDER_FILLED"
                    if order.cumulative_filled_quantity >= order.quantity
                    else "ORDER_PARTIALLY_FILLED"
                )
                self.audit.record(
                    event_type, source="futu_execution", account_id=intent["account_id"],
                    strategy_id=self.store.virtual_account(intent["account_id"])["strategy_id"],
                    channel="futu", decision_id=intent["decision_id"],
                    order_id=order.channel_order_id, correlation_id=intent["decision_id"],
                    details={
                        "side": order.side, "quantity": fill["quantity"],
                        "cumulative_quantity": order.cumulative_filled_quantity,
                        "average_fill_price": order.average_fill_price,
                    },
                )
            self.store.update_channel_order_report(order.channel_order_id, asdict(order))
            if order.status == "CANCELLED_ALL" and previous_status not in TERMINAL_ORDER_STATUSES:
                account = self.store.virtual_account(intent["account_id"])
                self.audit.record(
                    "CANCEL_SUCCEEDED", source="futu_execution",
                    account_id=intent["account_id"], channel="futu",
                    strategy_id=account["strategy_id"],
                    strategy_version=account["strategy_version"],
                    release_hash=account["release_hash"],
                    decision_id=intent["decision_id"], order_id=order.channel_order_id,
                    correlation_id=intent["decision_id"],
                    details={"status": order.status},
                )
            if (
                order.status in TERMINAL_ORDER_STATUSES
                and order.status != "FILLED_ALL"
                and previous_status not in TERMINAL_ORDER_STATUSES
            ):
                account = self.store.virtual_account(intent["account_id"])
                self.audit.record(
                    "ORDER_TERMINATED", source="futu_execution",
                    account_id=intent["account_id"], strategy_id=account["strategy_id"],
                    strategy_version=account["strategy_version"],
                    release_hash=account["release_hash"], channel="futu",
                    decision_id=intent["decision_id"], order_id=order.channel_order_id,
                    correlation_id=intent["decision_id"],
                    details={
                        "status": order.status,
                        "cumulative_quantity": order.cumulative_filled_quantity,
                    },
                )
        self._orders = orders
        # Positions and cash can change with the order fills processed above.
        self.refresh_account()
        if self._snapshot is not None:
            foreign_positions = [
                position for position in self._snapshot.positions
                if position.symbol != self.symbol and position.quantity != 0
            ]
            if foreign_positions:
                self.store.set_setting("channel_reconciliation_status", "BLOCKED")
                self.audit.record(
                    "CHANNEL_RECONCILIATION_FAILED", source="futu_execution",
                    outcome="FAILURE", channel="futu",
                    details={
                        "reason": "unowned_position",
                        "positions": [asdict(position) for position in foreign_positions],
                    },
                )
                raise ChannelReconciliationError("Futu账户存在PTE无法归属的持仓")
            broker_quantity = sum(
                position.quantity for position in self._snapshot.positions
                if position.symbol == self.symbol
            )
            logical_quantity = sum(
                int(account["quantity"]) for account in self.store.virtual_accounts()
                if account["symbol"] == self.symbol
            )
            if broker_quantity != logical_quantity:
                self.store.set_setting("channel_reconciliation_status", "BLOCKED")
                self.audit.record(
                    "CHANNEL_RECONCILIATION_FAILED", source="futu_execution",
                    outcome="FAILURE", channel="futu",
                    details={
                        "reason": "position_mismatch", "broker_quantity": broker_quantity,
                        "logical_quantity": logical_quantity,
                    },
                )
                raise ChannelReconciliationError(
                    f"Futu持仓与虚拟账户分账不一致: {broker_quantity}!={logical_quantity}"
                )
        self.store.set_setting("channel_reconciliation_status", "OK")
        if previous_reconciliation == "BLOCKED":
            self.audit.record(
                "CHANNEL_RECONCILIATION_RECOVERED", source="futu_execution",
                channel="futu", details={"reason": "orders_and_positions_reconciled"},
            )
        return self.status()

    def submit_pending(self, *, reconcile: bool = True):
        if self._draining:
            return self.status()
        if reconcile:
            self.refresh_orders()
        elif self._snapshot is None:
            self.refresh_account()
        if self.store.is_paused():
            return self.status()
        for row in self.store.pending_account_intents():
            account = self.store.virtual_account(row["account_id"])
            if bool(account["paused"]) or account["status"] != "RUNNING":
                continue
            if row["valid_session"] < self.today().isoformat():
                self.store.release_account_intent(row["intent_id"], "EXPIRED")
                self.audit.record(
                    "DECISION_EXPIRED", source="futu_execution", outcome="SKIPPED",
                    account_id=row["account_id"], channel="futu",
                    decision_id=row["decision_id"], correlation_id=row["decision_id"],
                    details={"intent_id": row["intent_id"], "valid_session": row["valid_session"]},
                )
                continue
            if row["valid_session"] != self.today().isoformat():
                continue
            intent = OrderIntent(
                row["intent_id"], row["decision_id"], row["symbol"], row["side"],
                int(row["quantity"]), float(row["limit_price"]), account_id=row["account_id"],
            )
            try:
                order = self.broker.place_order(intent)
            except Exception as exc:
                self.store.update_account_intent_status(
                    row["intent_id"], "SUBMISSION_UNCERTAIN"
                )
                self.store.set_virtual_health(
                    row["account_id"], "BLOCKED",
                    "Futu下单结果不确定，已保留冻结资金并等待订单对账",
                )
                self.audit.record(
                    "ORDER_SUBMISSION_FAILED", source="futu_execution", outcome="FAILURE",
                    account_id=row["account_id"], channel="futu",
                    decision_id=row["decision_id"], correlation_id=row["decision_id"],
                    details={"side": row["side"], "quantity": row["quantity"], "error": str(exc)},
                )
                raise
            submitted_event = self.audit.build(
                "ORDER_SUBMITTED", source="futu_execution", account_id=row["account_id"],
                strategy_id=account["strategy_id"], strategy_version=account["strategy_version"],
                release_hash=account["release_hash"], channel="futu",
                decision_id=row["decision_id"], order_id=order.channel_order_id,
                correlation_id=row["decision_id"],
                details={
                    "side": row["side"], "quantity": row["quantity"],
                    "limit_price": row["limit_price"], "status": order.status,
                },
            )
            self.store.bind_channel_order(
                row["intent_id"], order.channel_order_id, asdict(order), submitted_event,
            )
        return self.status()

    def refresh(self):
        self.refresh_orders()
        self.submit_pending(reconcile=False)
        return self.status()

    def status(self):
        account = None if self._snapshot is None else asdict(self._snapshot.account)
        positions = [] if self._snapshot is None else [asdict(row) for row in self._snapshot.positions]
        reconciliation = self.store.get_setting("channel_reconciliation_status")
        return {
            "environment": None if account is None else account["environment"],
            "market": None if account is None else account["market"],
            "symbol": self.symbol, "account": account, "positions": positions,
            "actual_quantity": sum(
                row["quantity"] for row in positions if row["symbol"] == self.symbol
            ),
            "orders": self.store.account_orders(),
            "quote_health": None if self._snapshot is None else self._snapshot.quote_health,
            "paused": self.store.is_paused(),
            "reconciliation_status": reconciliation,
            "alerts": (["CHANNEL_RECONCILIATION_BLOCKED"] if reconciliation == "BLOCKED" else []),
            "scheduler_failures": self.store.operation_failures(),
        }

    def pause(self):
        self.store.set_paused(True)
        return self.status()

    def resume(self):
        self.refresh_account()
        self.refresh_orders()
        self.store.set_paused(False)
        return self.status()

    def issue_cancel_token(self, account_id: str, channel_order_id: str) -> str:
        order = self.store.account_order(channel_order_id)
        if order["account_id"] != account_id:
            raise ChannelReconciliationError("撤单账户与订单归属不一致")
        token = secrets.token_urlsafe(24)
        expires = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
        self.store.save_cancel_token(token, channel_order_id, expires)
        return token

    def confirm_cancel(self, account_id: str, channel_order_id: str, token: str):
        order = self.store.account_order(channel_order_id)
        if order["account_id"] != account_id:
            raise ChannelReconciliationError("撤单账户与订单归属不一致")
        now = datetime.now(timezone.utc).isoformat()
        result = self.store.consume_cancel_token(token, channel_order_id, now)
        if result != "ok":
            raise ValueError("cancel token is invalid, expired, used, or belongs to another order")
        account = self.store.virtual_account(account_id)
        self.audit.record(
            "CANCEL_REQUESTED", source="futu_execution", actor_type="OPERATOR",
            account_id=account_id, channel="futu", decision_id=order["decision_id"],
            strategy_id=account["strategy_id"],
            strategy_version=account["strategy_version"],
            release_hash=account["release_hash"],
            order_id=channel_order_id, correlation_id=order["decision_id"], details={},
        )
        try:
            self.broker.cancel_order(channel_order_id)
        except Exception as exc:
            self.audit.record(
                "CANCEL_FAILED", source="futu_execution", outcome="FAILURE",
                actor_type="OPERATOR", account_id=account_id, channel="futu",
                strategy_id=account["strategy_id"],
                strategy_version=account["strategy_version"],
                release_hash=account["release_hash"],
                decision_id=order["decision_id"], order_id=channel_order_id,
                correlation_id=order["decision_id"],
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
            raise
        return self.status()

    def close(self):
        close = getattr(self.broker, "close", None)
        if close is not None:
            close()
        self.store.close()
