"""Account-owned strategy decisions and durable Futu order intents."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json

from .audit import AuditRecorder
from .broker import TERMINAL_INTENT_STATUSES
from .store import PaperStore


class AccountRefreshBatchError(RuntimeError):
    """One or more active virtual accounts failed to refresh."""


class ActiveOrderPendingError(RuntimeError):
    """A newer decision must wait until an older order reaches a known terminal state."""


class AccountEngine:
    _BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")

    def __init__(
        self, store: PaperStore, advice, audit: AuditRecorder | None = None,
        now=None,
    ) -> None:
        self.store = store
        self.advice = advice
        self.audit = audit or AuditRecorder(store)
        self.now = now or (lambda: datetime.now(self._BEIJING))
        self._draining = False

    def begin_shutdown(self) -> None:
        self._draining = True

    def _assign_decision_id(self, account_id: str, previous_payload, decision):
        source_id = decision.source_decision_id or decision.decision_id
        previous = json.loads(previous_payload) if previous_payload else None
        if previous:
            previous_source = previous.get("source_decision_id") or previous.get("decision_id")
            if previous_source == source_id:
                return replace(
                    decision,
                    decision_id=str(previous["decision_id"]),
                    source_decision_id=source_id,
                )

        generated_at = self.now()
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=self._BEIJING)
        else:
            generated_at = generated_at.astimezone(self._BEIJING)
        stamp = generated_at.strftime("%Y%m%d-%H%M")
        suffix = sha256(f"{account_id}\0{source_id}".encode("utf-8")).hexdigest()[:12].upper()
        return replace(
            decision,
            decision_id=f"DEC-{stamp}-{suffix}",
            source_decision_id=source_id,
        )

    def refresh_account(self, account_id: str, *, force: bool = False):
        account = self.store.virtual_account(account_id)
        previous_payload = account.get("last_decision_payload")
        active_intents = [
            row for row in self.store.account_intents(account_id)
            if row["status"] not in TERMINAL_INTENT_STATUSES
        ]
        if active_intents:
            previous = json.loads(previous_payload) if previous_payload else {}
            published_date = self.store.get_setting("last_data_publish_date")
            if published_date and published_date != previous.get("signal_date"):
                message = "存在未完成订单，新数据决策暂缓生成并等待对账"
                self.audit.record(
                    "ORDER_SUBMISSION_BLOCKED", source="account_engine", outcome="SKIPPED",
                    account_id=account_id, strategy_id=account["strategy_id"],
                    strategy_version=account["strategy_version"],
                    release_hash=account["release_hash"], symbol=account["symbol"],
                    correlation_id=f"publication:{published_date}",
                    details={
                        "reason": "previous_order_active",
                        "published_date": published_date,
                        "active_intent_ids": [row["intent_id"] for row in active_intents],
                    },
                )
                raise ActiveOrderPendingError(message)
            return self.status(account_id)
        previous_action = (
            json.loads(previous_payload).get("action") if previous_payload else None
        )
        def transform(value):
            return self._assign_decision_id(account_id, previous_payload, value)
        decision = self.advice.get_decision(
            int(account["quantity"]), float(account["cash"]),
            cycle_target_quantity=account["cycle_target"],
            strategy_id=account["strategy_id"],
            strategy_version=account["strategy_version"],
            account_id=account_id,
            symbol=account["symbol"],
            asset=account["asset_type"],
            decision_transform=transform,
        )
        if decision.decision_id == (decision.source_decision_id or decision.decision_id):
            decision = transform(decision)
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
            if decision.action in {"BUY", "SELL", "ROTATE"}:
                self.audit.record(
                    "SIGNAL_TRIGGERED", source="account_engine", **scope,
                    details={
                        "side": decision.action,
                        "quantity": (
                            decision.plan_legs[0].order.quantity
                            if decision.plan_legs else abs(decision.delta_quantity)
                        ),
                        "target_quantity": decision.target_quantity,
                        "valid_session": decision.valid_session.isoformat(),
                    },
                )
            elif decision.action in {"WAIT", "HOLD"} and previous_action in {
                "BUY", "SELL", "ROTATE",
            }:
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
            if decision.plan_legs:
                plan_events = []
                plan_rows = []
                for leg in decision.plan_legs:
                    order = leg.order
                    plan_events.append(self.audit.build(
                        "ORDER_INTENT_CREATED", source="account_engine", channel="futu",
                        **scope,
                        details={
                            "side": order.side,
                            "quantity": order.quantity,
                            "order_type": order.order_type,
                            "limit_price": order.limit_price,
                            "valid_session": decision.valid_session.isoformat(),
                            "order_sequence": leg.sequence,
                            "plan_mode": decision.plan_mode,
                            "role": leg.role,
                            "checkpoint": leg.checkpoint,
                            "submit_after": leg.submit_after.isoformat(),
                            "submit_before": leg.submit_before.isoformat(),
                            "dependency_sequence": leg.dependency_sequence,
                        },
                    ))
                    plan_rows.append({
                        "sequence": leg.sequence,
                        "plan_mode": decision.plan_mode,
                        "role": leg.role,
                        "checkpoint": leg.checkpoint,
                        "submit_after": leg.submit_after.isoformat(),
                        "submit_before": leg.submit_before.isoformat(),
                        "dependency_sequence": leg.dependency_sequence,
                        "dependency_required_status": leg.dependency_required_status,
                        "side": order.side,
                        "quantity": order.quantity,
                        "order_type": order.order_type,
                        "limit_price": order.limit_price,
                    })
                self.store.create_account_plan_intents(
                    account_id=account_id,
                    decision_id=decision.decision_id,
                    symbol=decision.symbol,
                    valid_session=decision.valid_session.isoformat(),
                    fee_rate=decision.fee_rate,
                    legs=plan_rows,
                    audit_events=plan_events,
                )
            for sequence, order in enumerate(decision.orders):
                intent_event = self.audit.build(
                    "ORDER_INTENT_CREATED", source="account_engine", channel="futu", **scope,
                    details={
                        "side": order.side, "quantity": order.quantity,
                        "order_type": order.order_type,
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
                    order_type=order.order_type,
                    audit_event=intent_event,
                )
        return self.status(account_id)

    def refresh_all(self):
        results = []
        failures = []
        for account in self.store.virtual_accounts():
            if account.get("status") == "RETIRED":
                continue
            try:
                results.append(self.refresh_account(account["account_id"]))
            except Exception as exc:
                failures.append((account["account_id"], exc))
                self.store.set_virtual_health(account["account_id"], "BLOCKED", str(exc))
                self.audit.record(
                    "VIRTUAL_ACCOUNT_FAILED", source="account_engine", outcome="FAILURE",
                    account_id=account["account_id"], strategy_id=account.get("strategy_id"),
                    strategy_version=account.get("strategy_version"),
                    release_hash=account.get("release_hash"),
                    details={"error": str(exc), "error_type": type(exc).__name__},
                )
        if failures:
            summary = "; ".join(
                f"{account_id}: {type(exc).__name__}: {exc}"
                for account_id, exc in failures
            )
            raise AccountRefreshBatchError(
                f"{len(failures)} virtual account refresh(es) failed: {summary}"
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
        all_snapshots = self.store.account_snapshots(account_id)
        snapshots = [
            row for row in all_snapshots
            if (start is None or row["session"] >= start)
            and (end is None or row["session"] <= end)
        ]
        baseline = float(account["initial_cash"])
        if start is not None:
            prior = [row for row in all_snapshots if row["session"] < start]
            if prior:
                baseline = float(prior[-1]["total_assets"])
        effective_end = end or (snapshots[-1]["session"] if snapshots else None)

        def fill_session(row) -> str:
            value = datetime.fromisoformat(str(row["occurred_at"]).replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=self._BEIJING)
            return value.astimezone(self._BEIJING).date().isoformat()

        fills = [] if not snapshots else [
            row for row in self.store.account_fills(account_id)
            if row["side"] == "SELL"
            and (start is None or fill_session(row) >= start)
            and fill_session(row) <= effective_end
        ]
        calculated = calculate_metrics(
            baseline, snapshots, closed_trade_pnl(fills),
        )
        return {
            "observation_start": snapshots[0]["session"] if snapshots else None,
            "observation_end": snapshots[-1]["session"] if snapshots else None,
            "baseline_assets": baseline,
            **calculated,
        }
