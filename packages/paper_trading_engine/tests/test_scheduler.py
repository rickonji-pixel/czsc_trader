from __future__ import annotations

from datetime import datetime


class Engine:
    def __init__(self):
        self.calls = []

    def refresh_orders(self):
        self.calls.append("orders")

    def refresh_account(self):
        self.calls.append("account")

    def refresh_decision_if_changed(self, *, force=False):
        self.calls.append("decision")


class Publisher:
    def __init__(self):
        self.calls = []

    def publish(self, end_date):
        self.calls.append(end_date)
        return {"ok": True}


class Store:
    def __init__(self):
        self.values = {}
        self.events = []

    def get_setting(self, key):
        return self.values.get(key)

    def set_setting(self, key, value):
        self.values[key] = value

    def add_event(self, kind, payload):
        self.events.append((kind, payload))


def test_scheduler_uses_distinct_order_account_decision_and_daily_publish_cadence() -> None:
    from paper_trading_engine.scheduler import RuntimeScheduler

    engine, publisher, store = Engine(), Publisher(), Store()
    scheduler = RuntimeScheduler(
        engine, publisher, store,
        order_interval=5, account_interval=60, decision_interval=5,
        publish_time="16:15",
    )
    scheduler.tick(datetime(2026, 9, 2, 16, 14, 0))
    scheduler.tick(datetime(2026, 9, 2, 16, 14, 5))
    scheduler.tick(datetime(2026, 9, 2, 16, 15, 0))
    scheduler.tick(datetime(2026, 9, 2, 16, 15, 5))

    assert engine.calls.count("orders") == 4
    assert engine.calls.count("account") == 2
    assert engine.calls.count("decision") == 4
    assert publisher.calls == ["2026-09-02"]
    assert store.values["last_data_publish_date"] == "2026-09-02"
    assert store.values["data_publication_error"] == ""


def test_failed_data_publication_waits_before_retrying() -> None:
    from paper_trading_engine.scheduler import RuntimeScheduler

    class FailingPublisher:
        def __init__(self):
            self.calls = 0

        def publish(self, end_date):
            self.calls += 1
            raise RuntimeError("vendor unavailable")

    engine, publisher, store = Engine(), FailingPublisher(), Store()
    scheduler = RuntimeScheduler(engine, publisher, store, publish_time="16:15")
    scheduler.tick(datetime(2026, 9, 2, 16, 15, 0))
    scheduler.tick(datetime(2026, 9, 2, 16, 15, 4))

    assert publisher.calls == 1
    assert store.events[-1][0] == "DATA_PUBLICATION_FAILED"
    assert store.values["data_publication_error"] == "vendor unavailable"


def test_default_publication_starts_at_1900_and_uses_bounded_backoff() -> None:
    from paper_trading_engine.scheduler import RuntimeScheduler

    class FailingPublisher:
        def __init__(self):
            self.calls = []

        def publish(self, end_date):
            self.calls.append(end_date)
            raise RuntimeError("not ready")

    engine, publisher, store = Engine(), FailingPublisher(), Store()
    scheduler = RuntimeScheduler(engine, publisher, store)
    scheduler.tick(datetime(2026, 9, 2, 18, 59, 59))
    scheduler.tick(datetime(2026, 9, 2, 19, 0, 0))
    scheduler.tick(datetime(2026, 9, 2, 19, 0, 4))
    scheduler.tick(datetime(2026, 9, 2, 19, 0, 5))
    scheduler.tick(datetime(2026, 9, 2, 19, 0, 19))
    scheduler.tick(datetime(2026, 9, 2, 19, 0, 20))

    assert publisher.calls == ["2026-09-02", "2026-09-02", "2026-09-02"]


def test_channel_failure_is_isolated_and_backed_off() -> None:
    from paper_trading_engine.scheduler import RuntimeScheduler

    class BrokenAccount(Engine):
        def refresh_account(self):
            self.calls.append("account")
            raise RuntimeError("network down")

    engine, store = BrokenAccount(), Store()
    scheduler = RuntimeScheduler(engine, Publisher(), store, order_interval=5, account_interval=5)
    scheduler.tick(datetime(2026, 9, 2, 10, 0, 0))
    scheduler.tick(datetime(2026, 9, 2, 10, 0, 1))
    scheduler.tick(datetime(2026, 9, 2, 10, 0, 5))
    scheduler.tick(datetime(2026, 9, 2, 10, 0, 10))
    scheduler.tick(datetime(2026, 9, 2, 10, 0, 20))

    assert engine.calls.count("account") == 3
    assert engine.calls.count("orders") == 4
    assert [event[0] for event in store.events].count("SCHEDULER_OPERATION_FAILED") == 1
    assert scheduler._failures["account"]["count"] == 3
