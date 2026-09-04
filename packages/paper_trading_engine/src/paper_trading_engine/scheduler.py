"""Independent cadences for observation, decisions, and daily data publication."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from threading import Event

from .audit import AuditRecorder


class RuntimeScheduler:
    def __init__(
        self,
        engine,
        publisher,
        store,
        *,
        order_interval: float = 5,
        account_interval: float = 60,
        decision_interval: float = 5,
        publish_time: str = "19:00",
        virtual_refresh=None,
        audit: AuditRecorder | None = None,
    ) -> None:
        self.engine = engine
        self.publisher = publisher
        self.store = store
        self.order_interval = float(order_interval)
        self.account_interval = float(account_interval)
        self.decision_interval = float(decision_interval)
        self.publish_time = time.fromisoformat(publish_time)
        self.virtual_refresh = virtual_refresh
        self.audit = audit or (
            AuditRecorder(store) if hasattr(store, "append_audit_event") else None
        )
        self._last_order: datetime | None = None
        self._last_account: datetime | None = None
        self._last_decision: datetime | None = None
        self._failures = self._restore_failures()
        self._retry_delays = (5, 15, 30, 60, 300)

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
            except (KeyError, TypeError, ValueError):
                continue
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
                self.store.add_event(
                    "SCHEDULER_OPERATION_FAILED",
                    {"operation": name, "error": str(exc), "failure_count": count,
                     "first_at": now.isoformat(), "retry_after_seconds": delay},
                )
            return False
        else:
            if name in self._failures:
                previous = self._failures.pop(name)
                self.store.add_event(
                    "SCHEDULER_OPERATION_RECOVERED",
                    {"operation": name, "previous_error": previous["fingerprint"],
                     "failure_count": previous["count"], "first_at": previous["first_at"].isoformat(),
                     "last_at": previous["last_at"].isoformat()},
                )
            clear = getattr(self.store, "clear_operation_failure", None)
            if clear is not None:
                clear(name)
            return True

    @staticmethod
    def _due(last: datetime | None, now: datetime, seconds: float) -> bool:
        return last is None or (now - last).total_seconds() >= seconds

    def tick(self, now: datetime) -> None:
        if self._due(self._last_account, now, self.account_interval):
            self._guard("account", now, self.engine.refresh_account)
            self._last_account = now
        if self._due(self._last_order, now, self.order_interval):
            self._guard("orders", now, self.engine.refresh_orders)
            self._last_order = now
        if self._due(self._last_decision, now, self.decision_interval):
            self._guard("decision", now, self.engine.refresh_decision_if_changed)
            self._last_decision = now
        today = now.date().isoformat()
        if (
            now.time() >= self.publish_time
            and self.store.get_setting("last_data_publish_date") != today
        ):
            def publish():
                correlation_id = f"publication:{today}"
                if self.audit is not None:
                    self.audit.record(
                        "MARKET_DATA_PUBLICATION_REQUESTED", source="scheduler",
                        actor_type="SCHEDULER", correlation_id=correlation_id,
                        details={"target_date": today},
                    )
                try:
                    result = self.publisher.publish(today)
                except Exception as exc:
                    self.store.set_setting("data_publication_error", str(exc))
                    if self.audit is not None:
                        self.audit.record(
                            "MARKET_DATA_PUBLICATION_FAILED", source="scheduler",
                            outcome="FAILURE", actor_type="SCHEDULER",
                            correlation_id=correlation_id,
                            details={"target_date": today, "error_type": type(exc).__name__,
                                     "error": str(exc)},
                        )
                    raise
                self.store.set_setting("last_data_publish_date", today)
                self.store.set_setting("data_publication_error", "")
                if self.audit is not None:
                    self.audit.record(
                        "MARKET_DATA_PUBLISHED", source="scheduler", actor_type="SCHEDULER",
                        correlation_id=correlation_id,
                        details={"target_date": today, "result": result},
                    )
            self._guard("publication", now, publish)
        if (
            self.virtual_refresh is not None
            and self.store.get_setting("last_data_publish_date") == today
            and self.store.get_setting("last_virtual_refresh_date") != today
        ):
            def refresh_virtual():
                self.virtual_refresh(now.date())
                self.store.set_setting("last_virtual_refresh_date", today)
            self._guard("virtual_accounts", now, refresh_virtual)

    def run(self, stopped: Event) -> None:
        while not stopped.is_set():
            try:
                self.tick(datetime.now())
            except Exception as exc:
                self.store.add_event("SCHEDULER_CYCLE_FAILED", {"error": str(exc)})
            stopped.wait(0.5)
