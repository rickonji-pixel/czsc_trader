"""Shared Futu execution and reconciliation for account-owned orders."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, time, timedelta, timezone
import math
import secrets

from .audit import AuditRecorder
from .broker import (
    BrokerOrderRejectedError,
    KNOWN_ORDER_STATUSES,
    OrderIntent,
    PaperTradingSafetyError,
    TERMINAL_INTENT_STATUSES,
    TERMINAL_ORDER_STATUSES,
)
from .trading_window import SHANGHAI, is_submission_window, shanghai_now


class ChannelReconciliationError(RuntimeError):
    pass


class FutuExecution:
    def __init__(
        self, store, broker, *, symbol: str | None = None, today=None, now=None, audit=None,
    ) -> None:
        self.store = store
        self.broker = broker
        self.symbol = symbol.upper() if symbol else None
        if now is None and today is not None:
            def test_clock():
                return datetime.combine(today(), time(10, 0), SHANGHAI)

            now = test_clock
        self.now = now or shanghai_now
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
        allocated = sum(
            float(row["initial_cash"])
            for row in self.store.virtual_accounts()
            if row.get("status") != "RETIRED"
        )
        capital_pool = float(self.store.get_setting("futu_capital_pool") or 0)
        if capital_pool <= 0 or allocated > capital_pool + 0.01:
            raise ChannelReconciliationError("虚拟账户分配资金超过PTE登记的Futu资金池")
        self._snapshot = snapshot
        self.store.mark_reconciled()
        return self.status()

    def _owned_intent(self, remark: str):
        return self.store.account_intent(remark) if remark.startswith("PTE-") else None

    def _block_reconciliation(
        self, reason: str, message: str, *, intent=None, order_id: str | None = None,
        details: dict | None = None,
    ) -> None:
        self.store.set_setting("channel_reconciliation_status", "BLOCKED")
        account_id = None if intent is None else intent["account_id"]
        if account_id:
            self.store.set_virtual_health(account_id, "BLOCKED", message)
        self.audit.record(
            "CHANNEL_RECONCILIATION_FAILED", source="futu_execution",
            outcome="FAILURE", channel="futu", account_id=account_id,
            decision_id=None if intent is None else intent["decision_id"],
            order_id=order_id,
            details={"reason": reason, **(details or {})},
        )
        raise ChannelReconciliationError(message)

    def _order_snapshot(self):
        by_id = {row.channel_order_id: row for row in self.broker.order_snapshot()}
        unresolved = self.store.unresolved_account_intents()
        active_orders = [
            row for row in self.store.account_orders()
            if row.get("status") not in TERMINAL_ORDER_STATUSES
        ]
        history = getattr(self.broker, "historical_order_snapshot", None)
        if history is not None and (unresolved or active_orders):
            dates = [str(row["valid_session"]) for row in unresolved]
            for row in active_orders:
                intent = self.store.account_intent(str(row["intent_id"]))
                if intent is not None:
                    dates.append(str(intent["valid_session"]))
            start = min(dates) if dates else (
                self.now().astimezone(SHANGHAI).date() - timedelta(days=30)
            ).isoformat()
            end = self.now().astimezone(SHANGHAI).date().isoformat()
            for row in history(start, end):
                by_id.setdefault(row.channel_order_id, row)
        return tuple(by_id.values())

    def _recover_transient_account_health(self, account_id: str) -> None:
        account = self.store.virtual_account(account_id)
        transient_prefixes = (
            "Futu下单结果不确定",
            "Futu已返回订单，但本地绑定失败",
            "Futu已返回订单，但订单回报与意图不一致",
            "Futu订单状态未知",
            "存在未完成订单",
        )
        if (
            account["health"] == "BLOCKED"
            and str(account.get("last_error") or "").startswith(transient_prefixes)
            and not any(
                row["status"] not in TERMINAL_INTENT_STATUSES
                for row in self.store.account_intents(account_id)
            )
            and not self.store.attention_account_intents(account_id)
        ):
            self.store.set_virtual_health(account_id, "OK")
            self.audit.record(
                "ACCOUNT_RECONCILIATION_RECOVERED", source="futu_execution",
                account_id=account_id, strategy_id=account.get("strategy_id"),
                strategy_version=account.get("strategy_version"),
                release_hash=account.get("release_hash"), channel="futu",
                details={"reason": "transient_order_state_resolved"},
            )

    @staticmethod
    def _validate_order(intent, order) -> None:
        intent_order_type = str(intent["payload"].get("order_type", "LIMIT")).upper()
        if order.status not in KNOWN_ORDER_STATUSES:
            raise ValueError(f"unknown Futu order status: {order.status}")
        if order.remark != intent["intent_id"]:
            raise ValueError("order remark differs from intent")
        if order.symbol.upper() != intent["symbol"].upper():
            raise ValueError("order symbol differs from intent")
        if order.side.upper() != intent["side"].upper():
            raise ValueError("order side differs from intent")
        if order.quantity != int(intent["quantity"]):
            raise ValueError("order quantity differs from intent")
        if order.order_type != intent_order_type:
            raise ValueError("order type differs from intent")
        if intent_order_type == "LIMIT" and not math.isclose(
            order.limit_price, float(intent["limit_price"]), abs_tol=1e-9,
        ):
            raise ValueError("order limit price differs from intent")
        if order.cumulative_filled_quantity < 0:
            raise ValueError("order cumulative fill cannot be negative")
        if order.cumulative_filled_quantity > int(intent["quantity"]):
            raise ValueError("order cumulative fill exceeds intent quantity")
        if order.cumulative_filled_quantity and (
            not math.isfinite(order.average_fill_price) or order.average_fill_price <= 0
        ):
            raise ValueError("filled order average price must be positive and finite")
        if (
            order.cumulative_filled_quantity
            and intent_order_type == "LIMIT"
            and order.side.upper() == "BUY"
            and order.average_fill_price > order.limit_price + 1e-9
        ):
            raise ValueError("buy average fill price exceeds limit")
        if (
            order.cumulative_filled_quantity
            and intent_order_type == "LIMIT"
            and order.side.upper() == "SELL"
            and order.average_fill_price < order.limit_price - 1e-9
        ):
            raise ValueError("sell average fill price is below limit")

    def refresh_orders(self):
        previous_reconciliation = self.store.get_setting("channel_reconciliation_status")
        orders = self._order_snapshot()
        seen_intents: set[str] = set()
        touched_accounts: set[str] = set()
        for order in orders:
            intent = self._owned_intent(order.remark)
            if intent is None:
                if order.status not in TERMINAL_ORDER_STATUSES:
                    self._block_reconciliation(
                        "unowned_order", f"Futu订单无法归属虚拟账户: {order.channel_order_id}",
                        order_id=order.channel_order_id, details={"remark": order.remark},
                    )
                continue
            try:
                self._validate_order(intent, order)
            except ValueError as exc:
                self._block_reconciliation(
                    "order_mismatch", f"Futu订单与本地意图不一致: {order.channel_order_id}",
                    intent=intent, order_id=order.channel_order_id,
                    details={"error": str(exc)},
                )
            seen_intents.add(intent["intent_id"])
            touched_accounts.add(intent["account_id"])
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
            if order.status == "TIMEOUT":
                self.store.set_virtual_health(
                    intent["account_id"], "BLOCKED",
                    "Futu订单状态未知（TIMEOUT），等待后续订单对账确认",
                )
            if order.status in {"CANCELLED_PART", "CANCELLED_ALL"} and (
                previous_status not in TERMINAL_ORDER_STATUSES
            ):
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
                self.store.set_virtual_health(
                    intent["account_id"], "BLOCKED",
                    f"Futu订单未完整执行（{order.status}），需要人工确认该次前瞻执行缺口",
                )
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
        missing = [
            row for row in self.store.account_intents()
            if row["status"] not in TERMINAL_INTENT_STATUSES
            and row["status"] != "PENDING_SUBMIT"
            and row["intent_id"] not in seen_intents
        ]
        if missing:
            intent = missing[0]
            self._block_reconciliation(
                "broker_order_missing",
                f"本地活动订单在Futu当前及历史订单中不存在: {intent['intent_id']}",
                intent=intent, order_id=intent.get("channel_order_id"),
                details={"intent_status": intent["status"]},
            )
        self._orders = orders
        # Positions and cash can change with the order fills processed above.
        self.refresh_account()
        if self._snapshot is not None:
            accounts = [
                account for account in self.store.virtual_accounts()
                if account.get("status") != "RETIRED"
            ]
            owned_symbols = {str(account["symbol"]).upper() for account in accounts}
            foreign_positions = [
                position for position in self._snapshot.positions
                if position.symbol not in owned_symbols and position.quantity != 0
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
            for symbol in sorted(owned_symbols):
                broker_quantity = sum(
                    position.quantity for position in self._snapshot.positions
                    if position.symbol == symbol
                )
                logical_quantity = sum(
                    int(account["quantity"]) for account in accounts
                    if account["symbol"] == symbol
                )
                if broker_quantity != logical_quantity:
                    self.store.set_setting("channel_reconciliation_status", "BLOCKED")
                    self.audit.record(
                        "CHANNEL_RECONCILIATION_FAILED", source="futu_execution",
                        outcome="FAILURE", channel="futu", symbol=symbol,
                        details={
                            "reason": "position_mismatch", "broker_quantity": broker_quantity,
                            "logical_quantity": logical_quantity,
                        },
                    )
                    raise ChannelReconciliationError(
                        f"Futu持仓与虚拟账户分账不一致({symbol}): "
                        f"{broker_quantity}!={logical_quantity}"
                    )
        violations = self.store.account_invariant_violations()
        if violations:
            first = violations[0]
            account_id = str(first["account_id"])
            self.store.set_virtual_health(
                account_id, "BLOCKED", f"虚拟账户账本与余额不一致: {account_id}",
            )
            intent = next(
                (row for row in self.store.account_intents(account_id)
                 if row["status"] not in TERMINAL_INTENT_STATUSES),
                None,
            )
            self._block_reconciliation(
                "account_ledger_mismatch",
                f"虚拟账户账本与余额不一致: {account_id}",
                intent=intent, details={"violations": violations},
            )
        self.store.set_setting("channel_reconciliation_status", "OK")
        if previous_reconciliation == "BLOCKED":
            self.audit.record(
                "CHANNEL_RECONCILIATION_RECOVERED", source="futu_execution",
                channel="futu", details={"reason": "orders_and_positions_reconciled"},
            )
        for account_id in sorted(touched_accounts):
            self._recover_transient_account_health(account_id)
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
        moment = self.now()
        if moment.tzinfo is None:
            raise ValueError("submission clock must be timezone-aware")
        session = moment.astimezone(SHANGHAI).date().isoformat()
        for row in self.store.pending_account_intents():
            account = self.store.virtual_account(row["account_id"])
            if (
                bool(account["paused"])
                or account["status"] != "RUNNING"
                or account["health"] not in {"READY", "OK"}
            ):
                continue
            if row["valid_session"] < session:
                message = "订单未在有效交易日内提交，已形成前瞻执行缺口"
                self.store.release_account_intent(
                    row["intent_id"], "EXPIRED", attention_reason=message,
                )
                self.store.set_virtual_health(row["account_id"], "BLOCKED", message)
                self.audit.record(
                    "DECISION_EXPIRED", source="futu_execution", outcome="SKIPPED",
                    account_id=row["account_id"], channel="futu",
                    decision_id=row["decision_id"], correlation_id=row["decision_id"],
                    details={"intent_id": row["intent_id"], "valid_session": row["valid_session"]},
                )
                continue
            if row["valid_session"] != session or not is_submission_window(moment):
                continue
            if not self.store.claim_account_intent(row["intent_id"]):
                continue
            intent = OrderIntent(
                row["intent_id"], row["decision_id"], row["symbol"], row["side"],
                int(row["quantity"]), float(row["limit_price"]),
                order_type=str(row["payload"].get("order_type", "LIMIT")),
                account_id=row["account_id"],
            )
            try:
                order = self.broker.place_order(intent)
            except BrokerOrderRejectedError as exc:
                self.store.release_account_intent(
                    row["intent_id"], "REJECTED", attention_reason=f"Futu明确拒单：{exc}",
                )
                self.store.set_virtual_health(
                    row["account_id"], "BLOCKED", f"Futu明确拒单：{exc}",
                )
                self.audit.record(
                    "ORDER_REJECTED", source="futu_execution", outcome="REJECTED",
                    account_id=row["account_id"], channel="futu",
                    decision_id=row["decision_id"], correlation_id=row["decision_id"],
                    details={"side": row["side"], "quantity": row["quantity"], "error": str(exc)},
                )
                continue
            except PaperTradingSafetyError as exc:
                self.store.release_account_intent(
                    row["intent_id"], "SUBMISSION_FAILED", attention_reason=str(exc),
                )
                self.store.set_virtual_health(row["account_id"], "BLOCKED", str(exc))
                self.audit.record(
                    "ORDER_SUBMISSION_FAILED", source="futu_execution", outcome="FAILURE",
                    account_id=row["account_id"], channel="futu",
                    decision_id=row["decision_id"], correlation_id=row["decision_id"],
                    details={"side": row["side"], "quantity": row["quantity"], "error": str(exc)},
                )
                continue
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
            try:
                self._validate_order(row, order)
            except ValueError as exc:
                self.store.update_account_intent_status(
                    row["intent_id"], "SUBMISSION_UNCERTAIN"
                )
                self.store.set_virtual_health(
                    row["account_id"], "BLOCKED",
                    f"Futu已返回订单，但订单回报与意图不一致：{exc}",
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
                    "order_type": row["payload"].get("order_type", "LIMIT"),
                    "limit_price": row["limit_price"], "status": order.status,
                },
            )
            try:
                self.store.bind_channel_order(
                    row["intent_id"], order.channel_order_id, asdict(order), submitted_event,
                )
            except Exception:
                self.store.update_account_intent_status(
                    row["intent_id"], "SUBMISSION_UNCERTAIN"
                )
                self.store.set_virtual_health(
                    row["account_id"], "BLOCKED",
                    "Futu已返回订单，但本地绑定失败，等待双向对账",
                )
                raise
        return self.status()

    def acknowledge_execution_gap(
        self, account_id: str, intent_id: str, resolution_note: str,
    ):
        """Resolve one reviewed execution gap after a fresh broker reconciliation."""
        self.refresh_orders()
        intent = self.store.account_intent(intent_id)
        if intent is None or intent["account_id"] != account_id:
            raise KeyError(intent_id)
        resolved = self.store.resolve_account_intent_attention(intent_id, resolution_note)
        if (
            not self.store.unresolved_account_intents(account_id)
            and not self.store.attention_account_intents(account_id)
        ):
            self.store.set_virtual_health(account_id, "OK")
        account = self.store.virtual_account(account_id)
        self.audit.record(
            "ACCOUNT_RECONCILIATION_RECOVERED", source="operator.reconciliation",
            actor_type="OPERATOR", account_id=account_id,
            strategy_id=account.get("strategy_id"),
            strategy_version=account.get("strategy_version"),
            release_hash=account.get("release_hash"), channel="futu",
            decision_id=intent["decision_id"],
            details={
                "reason": "execution_gap_acknowledged", "intent_id": intent_id,
                "resolution_note": resolution_note,
            },
        )
        return resolved

    def refresh(self):
        self.refresh_orders()
        self.submit_pending(reconcile=False)
        return self.status()

    def status(self):
        account = None if self._snapshot is None else asdict(self._snapshot.account)
        positions = [] if self._snapshot is None else [asdict(row) for row in self._snapshot.positions]
        reconciliation = self.store.get_setting("channel_reconciliation_status")
        symbols = sorted({row["symbol"] for row in self.store.virtual_accounts()})
        actual_by_symbol = {
            symbol: sum(row["quantity"] for row in positions if row["symbol"] == symbol)
            for symbol in symbols
        }
        return {
            "environment": None if account is None else account["environment"],
            "market": None if account is None else account["market"],
            "symbol": symbols[0] if len(symbols) == 1 else None,
            "symbols": symbols,
            "account": account, "positions": positions,
            "actual_quantity": sum(actual_by_symbol.values()),
            "actual_quantity_by_symbol": actual_by_symbol,
            "orders": self.store.account_orders(),
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
