"""Independent cadences for broker reconciliation and SRT data preparation."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import json
from threading import Event, Thread

from .audit import AuditRecorder
from .trading_window import SHANGHAI, shanghai_now


class RuntimeScheduler:
    def __init__(
        self,
        engine,
        prepared_data,
        store,
        *,
        order_interval: float = 5,
        account_interval: float = 60,
        data_observe_interval: float = 5,
        observation_time: str = "20:30",
        audit: AuditRecorder | None = None,
        initial_observation_at: datetime | None = None,
    ) -> None:
        self.engine = engine
        self.prepared_data = prepared_data
        self.store = store
        self.order_interval = float(order_interval)
        self.account_interval = float(account_interval)
        self.data_observe_interval = float(data_observe_interval)
        self.observation_time = time.fromisoformat(observation_time)
        self.audit = audit or (
            AuditRecorder(store) if hasattr(store, "append_audit_event") else None
        )
        self._last_order = initial_observation_at
        self._last_account = initial_observation_at
        self._last_heartbeat = initial_observation_at
        self._last_data_check: datetime | None = None
        self._daily_thread: Thread | None = None
        self.shutdown_clean = True
        self._failures = self._restore_failures()
        self._retry_delays = (5, 15, 30, 60, 300)
        self.store.set_setting("scheduler_started_at", datetime.now(timezone.utc).isoformat())
        self.store.set_setting("scheduler_heartbeat_at", datetime.now(timezone.utc).isoformat())

    def _restore_failures(self) -> dict[str, dict[str, object]]:
        load = getattr(self.store, "operation_failures", None)
        if load is None:
            return {}
        restored: dict[str, dict[str, object]] = {}
        for item in load():
            try:
                restored[str(item["operation"])] = {
                    "fingerprint": str(item["fingerprint"]),
                    "count": int(item["failure_count"]),
                    "first_at": datetime.fromisoformat(str(item["first_at"])),
                    "last_at": datetime.fromisoformat(str(item["last_at"])),
                    "next_retry": datetime.fromisoformat(str(item["next_retry"])),
                }
            except (KeyError, TypeError, ValueError) as exc:
                operation = (
                    item.get("operation", "<unknown>")
                    if isinstance(item, dict)
                    else "<unknown>"
                )
                raise ValueError(
                    f"invalid persisted scheduler failure: {operation}"
                ) from exc
        return restored

    def _guard(self, name: str, now: datetime, operation) -> bool:
        state = self._failures.get(name)
        if state is not None and now < state["next_retry"]:
            return False
        try:
            operation()
        except Exception as exc:
            fingerprint = f"{type(exc).__name__}:{exc}"
            previous = self._failures.get(name)
            count = 1 if previous is None or previous["fingerprint"] != fingerprint else int(previous["count"]) + 1
            first_at = now if count == 1 else previous["first_at"]
            delay = self._retry_delays[min(count - 1, len(self._retry_delays) - 1)]
            self._failures[name] = {
                "fingerprint": fingerprint, "count": count, "first_at": first_at,
                "last_at": now, "next_retry": now + timedelta(seconds=delay),
            }
            persist = getattr(self.store, "set_operation_failure", None)
            if persist is not None:
                persist(name, {
                    "error": str(exc), "fingerprint": fingerprint, "failure_count": count,
                    "first_at": first_at.isoformat(), "last_at": now.isoformat(),
                    "next_retry": (now + timedelta(seconds=delay)).isoformat(),
                })
            if count == 1:
                details = {"operation": name, "error": str(exc), "failure_count": count,
                           "first_at": now.isoformat(), "retry_after_seconds": delay}
                if self.audit is not None:
                    self.audit.record(
                        "SCHEDULER_OPERATION_FAILED", source="scheduler", outcome="FAILURE",
                        actor_type="SCHEDULER", details=details,
                    )
                else:
                    self.store.add_event("SCHEDULER_OPERATION_FAILED", details)
            return False
        else:
            self.store.set_setting(f"last_{name}_success_at", now.isoformat())
            if name in self._failures:
                previous = self._failures.pop(name)
                details = {"operation": name, "previous_error": previous["fingerprint"],
                           "failure_count": previous["count"],
                           "first_at": previous["first_at"].isoformat(),
                           "last_at": previous["last_at"].isoformat()}
                if self.audit is not None:
                    self.audit.record(
                        "SCHEDULER_OPERATION_RECOVERED", source="scheduler",
                        actor_type="SCHEDULER", details=details,
                    )
                else:
                    self.store.add_event("SCHEDULER_OPERATION_RECOVERED", details)
            clear = getattr(self.store, "clear_operation_failure", None)
            if clear is not None:
                clear(name)
            return True

    @staticmethod
    def _due(last: datetime | None, now: datetime, seconds: float) -> bool:
        return last is None or (now - last).total_seconds() >= seconds

    def _validated_prepared_data(
        self, result: object,
    ) -> tuple[str, dict[str, str]]:
        """Accept an observation only when every active instrument is authenticated."""
        if not isinstance(result, dict):
            raise ValueError("prepared-data observation must be an object")
        cutoff = result.get("prepared_through")
        try:
            cutoff = date.fromisoformat(str(cutoff)).isoformat()
        except (TypeError, ValueError) as exc:
            raise ValueError("prepared-data observation has no valid cutoff") from exc
        instruments = result.get("instruments")
        if not isinstance(instruments, list) or not instruments:
            raise ValueError("prepared-data observation has no instruments")
        identities: dict[str, str] = {}
        for item in instruments:
            if not isinstance(item, dict) or not isinstance(item.get("result"), dict):
                raise ValueError("prepared-data instrument result is invalid")
            symbol = str(item.get("symbol", "")).upper()
            identity = item["result"].get("data_identity")
            item_cutoff = item["result"].get("prepared_through")
            if not symbol or not isinstance(identity, str) or not identity:
                raise ValueError("prepared-data instrument identity is incomplete")
            if item_cutoff != cutoff:
                raise ValueError(f"{symbol}: instrument prepared-through date differs")
            if symbol in identities:
                raise ValueError(f"duplicate prepared-data instrument: {symbol}")
            identities[symbol] = identity
        expected = {
            str(account["symbol"]).upper()
            for account in self.store.strategy_virtual_accounts()
            if account.get("status") != "RETIRED"
        }
        if set(identities) != expected:
            raise ValueError(
                "prepared-data instruments differ from active accounts: "
                f"prepared={sorted(identities)}, expected={sorted(expected)}"
            )
        return cutoff, identities

    def tick(self, now: datetime) -> None:
        self.tick_fast(now)
        self.tick_daily(now)

    def _heartbeat(self, now: datetime) -> None:
        if self._due(self._last_heartbeat, now, 10):
            self.store.set_setting("scheduler_heartbeat_at", now.isoformat())
            self._last_heartbeat = now

    def tick_fast(self, now: datetime) -> None:
        self._heartbeat(now)
        if self._due(self._last_account, now, self.account_interval):
            self._guard("account", now, self.engine.refresh_account)
            self._last_account = now
        if self._due(self._last_order, now, self.order_interval):
            self._guard("orders", now, self.engine.refresh_orders)
            self._last_order = now

    def tick_daily(self, now: datetime) -> None:
        local_now = now if now.tzinfo is None else now.astimezone(SHANGHAI)
        if (
            local_now.time().replace(tzinfo=None) >= self.observation_time
            and self._due(
                self._last_data_check, now, self.data_observe_interval
            )
        ):
            def observe_prepared_data():
                try:
                    result = self.prepared_data.observe()
                    cutoff, identities = self._validated_prepared_data(result)
                except Exception as exc:
                    self.store.set_setting("data_preparation_error", str(exc))
                    if self.audit is not None:
                        self.audit.record(
                            "DATA_PREPARATION_OBSERVATION_FAILED", source="scheduler",
                            outcome="FAILURE", actor_type="SCHEDULER",
                            details={"error_type": type(exc).__name__, "error": str(exc)},
                        )
                    raise
                identity_json = json.dumps(identities, sort_keys=True)
                if identity_json == self.store.get_setting("last_prepared_data_ids"):
                    self.store.set_setting("data_preparation_error", "")
                    return
                correlation_id = f"prepared-data:{cutoff}"
                self.store.set_setting("last_data_prepare_date", cutoff)
                self.store.set_setting("last_prepared_data_ids", identity_json)
                self.store.set_setting("last_data_preparation", now.isoformat())
                self.store.set_setting("data_preparation_error", "")
                if self.audit is not None:
                    self.audit.record(
                        "MARKET_DATA_PREPARED", source="scheduler", actor_type="SCHEDULER",
                        correlation_id=correlation_id,
                        details={"prepared_through": cutoff, "result": result},
                    )
            self._guard("data_preparation_observation", now, observe_prepared_data)
            self._last_data_check = now
        prepared_through = self.store.get_setting("last_data_prepare_date")
        if (
            prepared_through is not None
            and self.store.get_setting("last_account_decision_date") != prepared_through
        ):
            def refresh_accounts():
                self.engine.refresh_decisions()
                self.store.set_setting("last_account_decision_date", prepared_through)
            self._guard("account_decisions", now, refresh_accounts)
        elif prepared_through is not None:
            pending = []
            for account in self.store.strategy_virtual_accounts():
                if account.get("status") == "RETIRED":
                    continue
                payload = account.get("last_decision_payload")
                try:
                    signal_date = json.loads(payload).get("signal_date") if payload else None
                except (json.JSONDecodeError, TypeError):
                    signal_date = None
                if signal_date is None:
                    pending.append(account)
            if pending:
                def onboard_accounts():
                    for account in pending:
                        self.engine.refresh_decision(str(account["account_id"]))
                self._guard("account_onboarding", now, onboard_accounts)

    def run(self, stopped: Event) -> None:
        def run_daily() -> None:
            while not stopped.is_set():
                try:
                    self.tick_daily(shanghai_now())
                except Exception as exc:
                    self._record_cycle_failure(exc, "daily")
                stopped.wait(0.5)

        self._daily_thread = Thread(
            target=run_daily, name="pte-daily-scheduler", daemon=True,
        )
        self._daily_thread.start()
        while not stopped.is_set():
            try:
                self.tick_fast(shanghai_now())
            except Exception as exc:
                self._record_cycle_failure(exc, "fast")
            stopped.wait(0.5)
        self._daily_thread.join(timeout=25.0)
        self.shutdown_clean = not self._daily_thread.is_alive()

    def _record_cycle_failure(self, exc: Exception, lane: str) -> None:
        details = {
            "error": str(exc), "error_type": type(exc).__name__, "lane": lane,
        }
        if self.audit is not None:
            self.audit.record(
                "SCHEDULER_CYCLE_FAILED", source="scheduler", outcome="FAILURE",
                actor_type="SCHEDULER", details=details,
            )
        else:
            self.store.add_event("SCHEDULER_CYCLE_FAILED", details)
