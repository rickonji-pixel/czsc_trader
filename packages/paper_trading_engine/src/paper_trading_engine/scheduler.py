"""Independent cadences for observation, decisions, and daily data publication."""

from __future__ import annotations

from datetime import datetime, time
from threading import Event


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
    ) -> None:
        self.engine = engine
        self.publisher = publisher
        self.store = store
        self.order_interval = float(order_interval)
        self.account_interval = float(account_interval)
        self.decision_interval = float(decision_interval)
        self.publish_time = time.fromisoformat(publish_time)
        self._last_order: datetime | None = None
        self._last_account: datetime | None = None
        self._last_decision: datetime | None = None
        self._last_publish_attempt: datetime | None = None

    @staticmethod
    def _due(last: datetime | None, now: datetime, seconds: float) -> bool:
        return last is None or (now - last).total_seconds() >= seconds

    def tick(self, now: datetime) -> None:
        if self._due(self._last_account, now, self.account_interval):
            self.engine.refresh_account()
            self._last_account = now
        if self._due(self._last_order, now, self.order_interval):
            self.engine.refresh_orders()
            self._last_order = now
        if self._due(self._last_decision, now, self.decision_interval):
            self.engine.refresh_decision_if_changed()
            self._last_decision = now
        today = now.date().isoformat()
        publish_due = self._due(self._last_publish_attempt, now, 300)
        if (
            now.time() >= self.publish_time
            and self.store.get_setting("last_data_publish_date") != today
            and publish_due
        ):
            self._last_publish_attempt = now
            try:
                result = self.publisher.publish(today)
            except Exception as exc:
                self.store.set_setting("data_publication_error", str(exc))
                self.store.add_event("DATA_PUBLICATION_FAILED", {"date": today, "error": str(exc)})
            else:
                self.store.set_setting("last_data_publish_date", today)
                self.store.set_setting("data_publication_error", "")
                self.store.add_event("DATA_PUBLISHED", {"date": today, "result": result})

    def run(self, stopped: Event) -> None:
        while not stopped.is_set():
            try:
                self.tick(datetime.now())
            except Exception as exc:
                self.store.add_event("SCHEDULER_CYCLE_FAILED", {"error": str(exc)})
            stopped.wait(0.5)
