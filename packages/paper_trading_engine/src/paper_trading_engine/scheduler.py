"""Independent cadences for observation, decisions, and daily data publication."""

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
        publisher,
        store,
        *,
        order_interval: float = 5,
        account_interval: float = 60,
        decision_interval: float = 5,
        publish_time: str = "20:30",
        audit: AuditRecorder | None = None,
    ) -> None:
        self.engine = engine
        self.publisher = publisher
        self.store = store
        self.order_interval = float(order_interval)
        self.account_interval = float(account_interval)
        # Kept as a compatibility argument for existing service definitions. Decisions are
        # generated once per successfully published data generation, never on a timer.
        self.decision_interval = float(decision_interval)
        self.publish_time = time.fromisoformat(publish_time)
        self.audit = audit or (
            AuditRecorder(store) if hasattr(store, "append_audit_event") else None
        )
        self._last_order: datetime | None = None
        self._last_account: datetime | None = None
        self._last_heartbeat: datetime | None = None
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

    def _validated_publication_result(
        self, result: object, requested_cutoff: str,
    ) -> tuple[str, dict[str, str]]:
        """Accept publication only when every active instrument has a fresh generation."""
        if not isinstance(result, dict):
            raise ValueError("data publication result must be an object")
        cutoff = result.get("data_cutoff")
        try:
            cutoff = date.fromisoformat(str(cutoff)).isoformat()
        except (TypeError, ValueError) as exc:
            raise ValueError("data publication result has no valid data_cutoff") from exc
        if cutoff != requested_cutoff:
            raise ValueError(
                f"data publication cutoff differs from request: {cutoff}!={requested_cutoff}"
            )
        instruments = result.get("instruments")
        if not isinstance(instruments, list) or not instruments:
            raise ValueError("data publication result has no instrument generations")
        generations: dict[str, str] = {}
        for item in instruments:
            if not isinstance(item, dict) or not isinstance(item.get("result"), dict):
                raise ValueError("data publication instrument result is invalid")
            symbol = str(item.get("symbol", "")).upper()
            generation = item["result"].get("generation_id")
            item_cutoff = item["result"].get("data_cutoff")
            if not symbol or not isinstance(generation, str) or not generation:
                raise ValueError("data publication instrument identity is incomplete")
            if item_cutoff != cutoff:
                raise ValueError(f"{symbol}: instrument cutoff differs from publication")
            if symbol in generations:
                raise ValueError(f"duplicate publication instrument: {symbol}")
            generations[symbol] = generation
        expected = {
            str(account["symbol"]).upper()
            for account in self.store.strategy_virtual_accounts()
            if account.get("status") != "RETIRED"
        }
        if set(generations) != expected:
            raise ValueError(
                "data publication instruments differ from active accounts: "
                f"published={sorted(generations)}, expected={sorted(expected)}"
            )
        return cutoff, generations

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
        today = local_now.date().isoformat()
        if (
            local_now.time().replace(tzinfo=None) >= self.publish_time
            and self.store.get_setting("last_data_publish_attempt_date") != today
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
                cutoff, generations = self._validated_publication_result(result, today)
                self.store.set_setting("last_data_publish_date", cutoff)
                self.store.set_setting(
                    "last_data_generation_ids", json.dumps(generations, sort_keys=True),
                )
                self.store.set_setting("last_data_publish_attempt_date", today)
                self.store.set_setting("last_data_publication", now.isoformat())
                self.store.set_setting("data_publication_error", "")
                if self.audit is not None:
                    self.audit.record(
                        "MARKET_DATA_PUBLISHED", source="scheduler", actor_type="SCHEDULER",
                        correlation_id=correlation_id,
                        details={"target_date": today, "data_cutoff": cutoff, "result": result},
                    )
            self._guard("publication", now, publish)
        published_date = self.store.get_setting("last_data_publish_date")
        if (
            published_date is not None
            and self.store.get_setting("last_account_decision_date") != published_date
        ):
            def refresh_accounts():
                self.engine.refresh_decisions()
                self.store.set_setting("last_account_decision_date", published_date)
            self._guard("account_decisions", now, refresh_accounts)
        elif published_date is not None:
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
                    result = self.publisher.publish(published_date)
                    cutoff, generations = self._validated_publication_result(
                        result, published_date,
                    )
                    self.store.set_setting("last_data_publish_date", cutoff)
                    self.store.set_setting(
                        "last_data_generation_ids",
                        json.dumps(generations, sort_keys=True),
                    )
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
