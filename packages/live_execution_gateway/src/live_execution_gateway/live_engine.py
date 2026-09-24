"""Fail-closed Longbridge live runtime with durable recovery and risk gates."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Event
from typing import Any, Callable
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from paper_trading_engine.srt_advice_client import SrtAdviceClient

from .live_store import LiveTradeStore
from .longbridge_readonly import LongbridgeReadOnlyGateway
from .longbridge_trading import (
    LiveOrderRequest,
    LongbridgeSubmissionUncertain,
    LongbridgeTradingGateway,
)
from .qualification import require_live_ready, strategy_identity
from .store import LiveObservationStore


_NEW_YORK = ZoneInfo("America/New_York")
_ORDER_STATUS = {
    "Filled": "FILLED",
    "PartialFilled": "PARTIAL_FILLED",
    "Rejected": "REJECTED",
    "Canceled": "CANCELED",
    "Expired": "EXPIRED",
    "PartialWithdrawal": "CANCELED_PARTIAL",
    "New": "SUBMITTED",
    "WaitToNew": "SUBMITTED",
    "NotReported": "SUBMITTED",
    "Replaced": "SUBMITTED",
    "PendingReplace": "REPLACE_PENDING",
    "WaitToReplace": "REPLACE_PENDING",
    "PendingCancel": "CANCEL_PENDING",
    "WaitToCancel": "CANCEL_PENDING",
    # The REST payload uses *Status suffixes; the Python SDK exposes shorter
    # enum class names. Both can appear in persisted broker observations.
    "FilledStatus": "FILLED",
    "PartialFilledStatus": "PARTIAL_FILLED",
    "RejectedStatus": "REJECTED",
    "CanceledStatus": "CANCELED",
    "ExpiredStatus": "EXPIRED",
    "NewStatus": "SUBMITTED",
    "ReplacedNotReported": "SUBMITTED",
    "ProtectedNotReported": "SUBMITTED",
    "VarietiesNotReported": "SUBMITTED",
    "ReplacedStatus": "SUBMITTED",
    "PendingReplaceStatus": "REPLACE_PENDING",
    "PendingCancelStatus": "CANCEL_PENDING",
}


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _whole_shares(value: object, name: str) -> int:
    quantity = _decimal(value)
    if quantity != quantity.to_integral_value() or quantity < 0:
        raise ValueError(f"Longbridge {name} is not a non-negative whole-share quantity")
    return int(quantity)


class LiveExecutionBlocked(RuntimeError):
    pass


class LongbridgeLiveEngine:
    def __init__(
        self,
        *,
        repo_root: str | Path,
        data_dir: str | Path,
        database: str | Path,
        env_file: str | Path,
        account_id: str,
        read_gateway: LongbridgeReadOnlyGateway | None = None,
        trading_gateway: LongbridgeTradingGateway | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.database = Path(database).resolve()
        self.env_file = Path(env_file).resolve()
        # StrategyInstance fetches declared inputs through DFLS. Keep credentials
        # process-local and let the provider read them from the standard env names.
        load_dotenv(self.env_file, override=False)
        self.account_id = account_id
        self.trade_store = LiveTradeStore(self.database)
        self.observation_store = LiveObservationStore(self.database)
        self.deployment = self.trade_store.deployment(account_id)
        self.read_gateway = read_gateway or LongbridgeReadOnlyGateway(
            symbol=self.deployment["symbol"], env_file=self.env_file,
        )
        self.trading_gateway = trading_gateway or LongbridgeTradingGateway(
            env_file=self.env_file,
        )
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.advice = SrtAdviceClient(
            repo_root=self.repo_root,
            data_dir=self.data_dir,
            symbol=self.deployment["symbol"],
            asset="stock",
            env_file=self.env_file,
        )
        self._last_snapshot: dict[str, object] | None = None

    def close(self) -> None:
        self.observation_store.close()
        self.trade_store.close()

    def current_identity(self) -> dict[str, Any]:
        identity = strategy_identity(
            self.repo_root,
            self.deployment["strategy_id"],
            self.deployment["strategy_version"],
        )
        if identity["release_hash"] != self.deployment["release_hash"]:
            raise LiveExecutionBlocked("live deployment release hash differs from strategy record")
        return identity

    def startup_reconcile(self, *, history_days: int = 30) -> dict[str, object]:
        history = self.read_gateway.history(days=history_days)
        self.observation_store.save_snapshot(history)
        snapshot = self.read_gateway.snapshot()
        self.observation_store.save_snapshot(snapshot)
        self._last_snapshot = snapshot
        return self.reconcile(snapshot, history=history)

    def _account_facts(self, snapshot: dict[str, object]) -> dict[str, object]:
        balances = list(snapshot.get("balances") or [])
        if len(balances) != 1:
            raise LiveExecutionBlocked("Longbridge must return exactly one USD account balance")
        usd_cash = [
            item
            for item in balances[0].get("cash_infos", [])
            if str(item.get("currency", "")).upper() == "USD"
        ]
        if len(usd_cash) != 1:
            raise LiveExecutionBlocked("Longbridge must return exactly one USD cash record")
        positions = [
            item for item in snapshot.get("positions", [])
            if str(item.get("symbol", "")).upper() == self.deployment["symbol"]
        ]
        if len(positions) > 1:
            raise LiveExecutionBlocked("Longbridge returned duplicate symbol positions")
        position = positions[0] if positions else {}
        return {
            "available_cash": _decimal(usd_cash[0]["available_cash"]),
            "total_assets": _decimal(balances[0]["net_assets"]),
            "quantity": _whole_shares(position.get("quantity", 0), "position"),
            "available_quantity": _whole_shares(
                position.get("available_quantity", 0), "available position",
            ),
        }

    def plan(self, snapshot: dict[str, object] | None = None) -> dict[str, object]:
        snapshot = snapshot or self._last_snapshot or self.read_gateway.snapshot()
        self._last_snapshot = snapshot
        facts = self._account_facts(snapshot)
        identity = self.current_identity()
        signal_date = self.advice.latest_completed_signal_date(at=self.now())
        prepared = self.advice.prepare_account_data(
            account_id=self.account_id,
            strategy_id=self.deployment["strategy_id"],
            strategy_version=self.deployment["strategy_version"],
            symbol=self.deployment["symbol"],
            asset="stock",
            signal_date=signal_date,
        )
        if prepared is None:
            raise LiveExecutionBlocked("no US trading session follows the completed signal")
        decision = self.advice.get_decision(
            int(facts["quantity"]),
            float(facts["available_cash"]),
            float(facts["total_assets"]),
            trading_date=prepared.tradable_window.start,
            portfolio_revision=0,
            state_revision=0,
            strategy_id=self.deployment["strategy_id"],
            strategy_version=self.deployment["strategy_version"],
            account_id=self.account_id,
            symbol=self.deployment["symbol"],
            asset="stock",
            prepared=prepared,
        )
        payload = asdict(decision)
        decision_strategy = payload.get("strategy") or {}
        expected_strategy = {
            "strategy_id": identity["strategy_id"],
            "version": identity["strategy_version"],
            "release_hash": identity["release_hash"],
            "qualification": identity["qualification"],
        }
        actual_strategy = {
            key: decision_strategy.get(key) for key in expected_strategy
        }
        if actual_strategy != expected_strategy:
            raise LiveExecutionBlocked(
                "SRT decision identity or qualification differs from the live deployment"
            )
        executable = identity["qualification"] == "LIVE_READY" and self.trade_store.armed(
            self.account_id
        )
        result = self.trade_store.save_decision_and_intents(
            account_id=self.account_id,
            decision=payload,
            qualification=identity["qualification"],
            executable=executable,
        )
        result.update({
            "qualification": identity["qualification"],
            "live_trading_armed": self.trade_store.armed(self.account_id),
            "broker_quantity": facts["quantity"],
            "broker_available_cash": str(facts["available_cash"]),
        })
        return result

    @staticmethod
    def _order_matches_intent(order: dict[str, object], intent: dict[str, Any]) -> bool:
        side = str(order.get("side", "")).upper()
        if side in {"BUY", "SELL"}:
            normalized_side = side
        else:
            normalized_side = "BUY" if side.endswith("BUY") else "SELL" if side.endswith("SELL") else side
        outside = str(order.get("outside_rth") or "")
        outside = {
            "RTHOnly": "RTH_ONLY", "AnyTime": "ANY_TIME", "Overnight": "OVERNIGHT",
        }.get(outside, outside)
        if outside and outside != intent.get("outside_rth", "RTH_ONLY"):
            return False
        if not outside and intent.get("outside_rth", "RTH_ONLY") != "RTH_ONLY":
            return False
        broker_type = str(order.get("order_type") or "")
        if broker_type and broker_type != {"MARKET": "MO", "LIMIT": "LO"}.get(
            intent["order_type"]
        ):
            return False
        return (
            str(order.get("symbol", "")).upper() == intent["symbol"]
            and normalized_side == intent["side"]
            and _whole_shares(order.get("quantity", 0), "order") == int(intent["quantity"])
        )

    def reconcile(
        self, snapshot: dict[str, object], *, history: dict[str, object] | None = None,
    ) -> dict[str, object]:
        orders = [*snapshot.get("today_orders", [])]
        if history is not None:
            orders.extend(history.get("orders", []))
        else:
            # Today's endpoint can omit a still-unresolved order after the date
            # rolls over. Consult broker history before declaring it missing.
            known = {str(order.get("remark", "")) for order in orders}
            unresolved = self.trade_store.unresolved_intents(self.account_id)
            if any(intent["intent_id"] not in known for intent in unresolved):
                history = self.read_gateway.history(days=30)
                self.observation_store.save_snapshot(history)
                orders.extend(history.get("orders", []))
        by_remark: dict[str, list[dict[str, object]]] = {}
        for order in orders:
            remark = str(order.get("remark", ""))
            if remark.startswith("LBI-"):
                candidates = by_remark.setdefault(remark, [])
                if not any(item.get("order_id") == order.get("order_id") for item in candidates):
                    candidates.append(order)
        recovered = 0
        for intent in self.trade_store.unresolved_intents(self.account_id):
            matches = by_remark.get(intent["intent_id"], [])
            if len(matches) > 1:
                message = "同一实盘意图在券商侧对应多个订单，账户已阻塞"
                self.trade_store.mark_intent(
                    intent["intent_id"], "RECONCILIATION_BLOCKED", error=message,
                )
                self.trade_store.disarm(self.account_id, reason=message, blocked=True)
                raise LiveExecutionBlocked(message)
            if not matches:
                last_known = datetime.fromisoformat(
                    intent.get("updated_at") or intent.get("submitted_at")
                )
                if self.now() - last_known >= timedelta(minutes=10):
                    message = "十分钟内券商当前与历史订单均未找到本地未决订单，禁止自动继续"
                    self.trade_store.mark_intent(
                        intent["intent_id"], "RECONCILIATION_BLOCKED", error=message,
                    )
                    self.trade_store.disarm(self.account_id, reason=message, blocked=True)
                continue
            order = matches[0]
            bound_order_id = str(intent.get("broker_order_id") or "")
            if bound_order_id and bound_order_id != str(order.get("order_id") or ""):
                message = "券商订单ID与本地已绑定订单不一致"
                self.trade_store.mark_intent(
                    intent["intent_id"], "RECONCILIATION_BLOCKED", error=message,
                )
                self.trade_store.disarm(self.account_id, reason=message, blocked=True)
                raise LiveExecutionBlocked(message)
            if not self._order_matches_intent(order, intent):
                message = "券商订单与本地持久化意图不一致"
                self.trade_store.mark_intent(intent["intent_id"], "RECONCILIATION_BLOCKED", error=message)
                self.trade_store.disarm(self.account_id, reason=message, blocked=True)
                raise LiveExecutionBlocked(message)
            status = _ORDER_STATUS.get(str(order.get("status")), "BROKER_STATUS_UNKNOWN")
            if (
                intent["status"] in {"CANCEL_PENDING", "CANCEL_UNCERTAIN"}
                and status in {"SUBMITTED", "PARTIAL_FILLED", "CANCEL_PENDING"}
            ):
                status = intent["status"]
            self.trade_store.bind_order(
                intent["intent_id"], str(order["order_id"]), status=status,
            )
            if status == "BROKER_STATUS_UNKNOWN":
                message = f"未知Longbridge订单状态: {order.get('status')}"
                self.trade_store.disarm(self.account_id, reason=message, blocked=True)
                raise LiveExecutionBlocked(message)
            recovered += 1
        return {"broker_orders_seen": len(orders), "intents_reconciled": recovered}

    def _submission_window(self, intent: dict[str, Any]) -> bool:
        local = self.now().astimezone(_NEW_YORK)
        clock = local.time().replace(tzinfo=None)
        session = date.fromisoformat(intent["valid_session"])
        outside = intent.get("outside_rth", "RTH_ONLY")
        if outside == "RTH_ONLY":
            return local.date() == session and time(9, 30) <= clock < time(16)
        if intent["order_type"] != "LIMIT":
            return False
        if outside == "ANY_TIME":
            return local.date() == session and time(4) <= clock < time(20)
        if outside == "OVERNIGHT":
            return (
                session.weekday() < 5
                and (
                    (local.date() + timedelta(days=1) == session and clock >= time(20))
                    or (local.date() == session and clock < time(3, 50))
                )
            )
        return False

    def _risk_check(self, intent: dict[str, Any], snapshot: dict[str, object]) -> None:
        identity = self.current_identity()
        require_live_ready(identity)
        if not self.trade_store.armed(self.account_id):
            raise LiveExecutionBlocked("Longbridge live deployment is not armed")
        deployment = self.trade_store.deployment(self.account_id)
        if int(intent["arm_generation"]) != int(deployment["arm_generation"]):
            raise LiveExecutionBlocked("live intent belongs to an older arm generation")
        if intent["symbol"] != deployment["symbol"]:
            raise LiveExecutionBlocked("live intent escaped symbol allowlist")
        if not self._submission_window(intent):
            raise LiveExecutionBlocked("live intent is outside its selected US trading session")
        facts = self._account_facts(snapshot)
        price = _decimal(intent["reference_price"])
        quantity = int(intent["quantity"])
        notional = (price * quantity).quantize(Decimal("0.01"))
        if notional > _decimal(deployment["max_order_notional"]):
            raise LiveExecutionBlocked("live order exceeds max_order_notional")
        current = int(facts["quantity"])
        resulting = current + quantity if intent["side"] == "BUY" else current - quantity
        if resulting < 0 or price * resulting > _decimal(deployment["max_gross_notional"]):
            raise LiveExecutionBlocked("live order exceeds position or max_gross_notional")
        if intent["side"] == "SELL" and quantity > int(facts["available_quantity"]):
            raise LiveExecutionBlocked("live sell exceeds available Longbridge position")
        if intent["side"] == "BUY":
            required = notional * Decimal("1.01") + _decimal(deployment["cash_reserve"])
            if required > _decimal(facts["available_cash"]):
                raise LiveExecutionBlocked("cash-only live buy exceeds available USD cash plus reserve")
        history = self.read_gateway.history(days=30)
        self.observation_store.save_snapshot(history)
        active_orders = [
            order for order in [*snapshot.get("today_orders", []), *history.get("orders", [])]
            if str(order.get("symbol", "")).upper() == deployment["symbol"]
            and _ORDER_STATUS.get(str(order.get("status")), "BROKER_STATUS_UNKNOWN") in {
                "SUBMITTED", "PARTIAL_FILLED", "REPLACE_PENDING", "CANCEL_PENDING",
                "BROKER_STATUS_UNKNOWN",
            }
        ]
        if active_orders:
            raise LiveExecutionBlocked(
                "Longbridge already has an active order for this symbol; new submission is blocked"
            )

    def submit_ready(self, snapshot: dict[str, object]) -> dict[str, object] | None:
        ready = self.trade_store.ready_intents(self.account_id)
        if not ready:
            return None
        intent = ready[0]
        local_date = self.now().astimezone(_NEW_YORK).date().isoformat()
        if intent["valid_session"] < local_date:
            message = "实盘订单已错过有效交易日，账户已阻塞等待人工复核"
            self.trade_store.mark_intent(intent["intent_id"], "EXPIRED", error=message)
            self.trade_store.disarm(self.account_id, reason=message, blocked=True)
            raise LiveExecutionBlocked(message)
        self._risk_check(intent, snapshot)
        claimed = self.trade_store.claim_intent(intent["intent_id"])
        if claimed is None:
            return None
        request = LiveOrderRequest(
            intent_id=claimed["intent_id"],
            client_request_id=claimed["client_request_id"],
            symbol=claimed["symbol"], side=claimed["side"],
            quantity=int(claimed["quantity"]), order_type=claimed["order_type"],
            time_in_force=claimed["time_in_force"],
            reference_price=_decimal(claimed["reference_price"]),
            outside_rth=claimed.get("outside_rth", "RTH_ONLY"),
        )
        try:
            order_id = self.trading_gateway.submit(request)
        except LongbridgeSubmissionUncertain as exc:
            message = f"Longbridge下单结果不确定，已停用实盘: {exc}"
            self.trade_store.mark_intent(
                claimed["intent_id"], "SUBMISSION_UNCERTAIN", error=message,
            )
            self.trade_store.disarm(self.account_id, reason=message, blocked=True)
            self.trade_store.event(
                "LIVE_ORDER_SUBMISSION_UNCERTAIN", account_id=self.account_id,
                intent_id=claimed["intent_id"], outcome="FAILURE",
                details={"error": str(exc)},
            )
            raise
        self.trade_store.bind_order(claimed["intent_id"], order_id)
        self.trade_store.event(
            "LIVE_ORDER_SUBMITTED", account_id=self.account_id,
            intent_id=claimed["intent_id"], details={
                "order_id": order_id, "client_request_id": claimed["client_request_id"],
                "symbol": claimed["symbol"], "side": claimed["side"],
                "quantity": claimed["quantity"], "order_type": claimed["order_type"],
                "outside_rth": claimed.get("outside_rth", "RTH_ONLY"),
            },
        )
        return {"intent_id": claimed["intent_id"], "order_id": order_id}

    def cycle(self) -> dict[str, object]:
        snapshot = self.read_gateway.snapshot()
        self.observation_store.save_snapshot(snapshot)
        self._last_snapshot = snapshot
        reconciliation = self.reconcile(snapshot)
        planning = self.plan(snapshot)
        submission = self.submit_ready(snapshot)
        return {
            "reconciliation": reconciliation,
            "planning": planning,
            "submission": submission,
            "status": self.trade_store.status(self.account_id),
        }

    def serve(self, *, interval: float = 15.0, stop: Event | None = None) -> None:
        if interval < 5:
            raise ValueError("live service interval must be at least five seconds")
        stop = stop or Event()
        self.startup_reconcile()
        while not stop.is_set():
            try:
                self.cycle()
            except Exception as exc:
                self.trade_store.event(
                    "LIVE_CYCLE_FAILED", account_id=self.account_id, outcome="FAILURE",
                    details={"error_type": type(exc).__name__, "error": str(exc)},
                )
            stop.wait(interval)

    def cancel(self, order_id: str, *, confirmation: str) -> None:
        if confirmation != "CANCEL_LONG_BRIDGE_REAL_ORDER":
            raise PermissionError("real-order cancellation confirmation differs")
        intent = self.trade_store.intent_by_order(self.account_id, order_id)
        if intent is None:
            raise PermissionError("only an order owned by this live deployment can be canceled")
        if intent["status"] not in {"SUBMITTED", "PARTIAL_FILLED", "CANCEL_UNCERTAIN"}:
            raise PermissionError("the tracked order is not in a cancelable local state")
        try:
            self.trading_gateway.cancel(order_id)
        except LongbridgeSubmissionUncertain as exc:
            message = f"Longbridge撤单结果不确定，已停用新实盘订单: {exc}"
            self.trade_store.mark_intent(
                intent["intent_id"], "CANCEL_UNCERTAIN", error=message,
            )
            self.trade_store.disarm(self.account_id, reason=message, blocked=True)
            self.trade_store.event(
                "LIVE_ORDER_CANCEL_UNCERTAIN", account_id=self.account_id,
                intent_id=intent["intent_id"], outcome="FAILURE",
                details={"order_id": order_id, "error": str(exc)},
            )
            raise
        self.trade_store.mark_intent(intent["intent_id"], "CANCEL_PENDING")
        self.trade_store.event(
            "LIVE_ORDER_CANCEL_REQUESTED", account_id=self.account_id,
            intent_id=intent["intent_id"], details={"order_id": order_id},
        )
