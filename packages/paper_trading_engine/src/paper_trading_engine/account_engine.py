"""Account-owned strategy decisions and durable Futu order intents."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
import json

from .audit import AuditRecorder
from .store import PaperStore


class AccountEngine:
    def __init__(self, store: PaperStore, advice, audit: AuditRecorder | None = None) -> None:
        self.store = store
        self.advice = advice
        self.audit = audit or AuditRecorder(store)
        self._draining = False

    def begin_shutdown(self) -> None:
        self._draining = True

    def refresh_account(self, account_id: str, *, force: bool = False):
        account = self.store.virtual_account(account_id)
        previous_payload = account.get("last_decision_payload")
        previous_action = (
            json.loads(previous_payload).get("action") if previous_payload else None
        )
        decision = self.advice.get_decision(
            int(account["quantity"]), float(account["cash"]),
            cycle_target_quantity=account["cycle_target"],
            strategy_id=account["strategy_id"],
            strategy_version=account["strategy_version"],
        )
        expected = (
            account["strategy_id"], account["strategy_version"], account["release_hash"],
        )
        actual = (
            decision.strategy.get("strategy_id"), decision.strategy.get("version"),
            decision.strategy.get("release_hash"),
        )
        if actual != expected:
            raise ValueError("advice strategy release differs from account")
        if decision.symbol != account["symbol"]:
            raise ValueError("advice symbol differs from account")
        if decision.actual_quantity != int(account["quantity"]):
            raise ValueError("advice quantity differs from account")

        uncertain_submission = any(
            row["status"] == "SUBMISSION_UNCERTAIN"
            for row in self.store.account_intents(account_id)
        )
        if account["health"] == "BLOCKED" and not uncertain_submission:
            self.store.set_virtual_health(account_id, "OK")
            account["health"] = "OK"

        payload = asdict(decision)
        self.store.save_account_decision(account_id, payload)
        valued_account = self.store.virtual_account(account_id)
        close = Decimal(str(decision.signal_reference_price))
        cash = Decimal(valued_account["cash"])
        frozen_cash = Decimal(valued_account["frozen_cash"])
        market_value = (close * int(valued_account["quantity"])).quantize(Decimal("0.0001"))
        total_assets = (cash + frozen_cash + market_value).quantize(Decimal("0.0001"))
        self.store.save_account_snapshot(
            account_id, decision.signal_date.isoformat(),
            {
                "cash": str(cash), "frozen_cash": str(frozen_cash),
                "quantity": int(valued_account["quantity"]), "close": str(close),
                "market_value": str(market_value), "total_assets": str(total_assets),
                "decision_id": decision.decision_id,
            },
        )
        scope = {
            "account_id": account_id, "strategy_id": account["strategy_id"],
            "strategy_version": account["strategy_version"],
            "release_hash": account["release_hash"], "symbol": account["symbol"],
            "decision_id": decision.decision_id, "correlation_id": decision.decision_id,
        }
        if not self.store.has_audit_event(
            "DECISION_GENERATED", account_id=account_id,
            decision_id=decision.decision_id, channel_is_null=True,
        ):
            self.audit.record(
                "DECISION_GENERATED", source="account_engine", **scope,
                details={
                    "action": decision.action,
                    "actual_quantity": decision.actual_quantity,
                    "target_quantity": decision.target_quantity,
                    "execution_reference_price": decision.execution_reference_price,
                    "signal_date": decision.signal_date.isoformat(),
                    "valid_session": decision.valid_session.isoformat(),
                },
            )
            if decision.action in {"BUY", "SELL"}:
                self.audit.record(
                    "SIGNAL_TRIGGERED", source="account_engine", **scope,
                    details={
                        "side": decision.action, "quantity": abs(decision.delta_quantity),
                        "target_quantity": decision.target_quantity,
                        "valid_session": decision.valid_session.isoformat(),
                    },
                )
            elif decision.action == "WAIT" and previous_action in {"BUY", "SELL"}:
                self.audit.record(
                    "SIGNAL_CLEARED", source="account_engine", **scope,
                    details={
                        "previous_action": previous_action,
                        "target_quantity": decision.target_quantity,
                        "valid_session": decision.valid_session.isoformat(),
                    },
                )

        if (
            not bool(account["paused"])
            and account["status"] == "RUNNING"
            and account["health"] != "BLOCKED"
            and not self._draining
        ):
            for sequence, order in enumerate(decision.orders):
                intent_event = self.audit.build(
                    "ORDER_INTENT_CREATED", source="account_engine", channel="futu", **scope,
                    details={
                        "side": order.side, "quantity": order.quantity,
                        "limit_price": order.limit_price,
                        "valid_session": decision.valid_session.isoformat(),
                        "order_sequence": sequence,
                    },
                )
                self.store.create_account_intent(
                    account_id=account_id, decision_id=decision.decision_id,
                    order_sequence=sequence, symbol=decision.symbol, side=order.side,
                    quantity=order.quantity, limit_price=order.limit_price,
                    valid_session=decision.valid_session.isoformat(), fee_rate=decision.fee_rate,
                    audit_event=intent_event,
                )
        return self.status(account_id)

    def refresh_all(self):
        results = []
        for account in self.store.virtual_accounts():
            if account.get("status") == "RETIRED":
                continue
            try:
                results.append(self.refresh_account(account["account_id"]))
            except Exception as exc:
                self.store.set_virtual_health(account["account_id"], "BLOCKED", str(exc))
                self.audit.record(
                    "VIRTUAL_ACCOUNT_FAILED", source="account_engine", outcome="FAILURE",
                    account_id=account["account_id"], strategy_id=account.get("strategy_id"),
                    strategy_version=account.get("strategy_version"),
                    release_hash=account.get("release_hash"),
                    details={"error": str(exc), "error_type": type(exc).__name__},
                )
        return results

    def status(self, account_id: str):
        account = self.store.virtual_account(account_id)
        payload = account.pop("last_decision_payload", None)
        return {
            **account,
            "last_decision": json.loads(payload) if payload else None,
            "orders": self.store.account_orders(account_id),
            "fills": self.store.account_fills(account_id),
            "snapshots": self.store.account_snapshots(account_id),
            "metrics": self.metrics(account_id),
        }

    def metrics(self, account_id: str, start: str | None = None, end: str | None = None):
        from .performance_export import calculate_metrics, closed_trade_pnl

        account = self.store.virtual_account(account_id)
        snapshots = [
            row for row in self.store.account_snapshots(account_id)
            if (start is None or row["session"] >= start)
            and (end is None or row["session"] <= end)
        ]
        initial = float(account["initial_cash"])
        fills = [
            row for row in self.store.account_fills(account_id)
            if row["side"] == "SELL"
            and (start is None or row["occurred_at"][:10] >= start)
            and (end is None or row["occurred_at"][:10] <= end)
        ]
        calculated = calculate_metrics(
            initial, snapshots, closed_trade_pnl(fills),
        )
        return {
            "observation_start": snapshots[0]["session"] if snapshots else None,
            "observation_end": snapshots[-1]["session"] if snapshots else None,
            **calculated,
        }
