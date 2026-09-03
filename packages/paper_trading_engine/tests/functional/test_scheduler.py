from datetime import datetime, timedelta, timezone

from paper_trading_engine.scheduler import RuntimeScheduler
from paper_trading_engine.trading_window import is_submission_window


class Engine:
    def __init__(self): self.calls = []
    def refresh_orders(self): self.calls.append("orders")
    def refresh_account(self): self.calls.append("account")
    def refresh_decision_if_changed(self, *, force=False): self.calls.append("decision")


class Store:
    def __init__(self): self.values, self.events, self.failures = {}, [], {}
    def get_setting(self, key): return self.values.get(key)
    def set_setting(self, key, value): self.values[key] = value
    def add_event(self, kind, payload): self.events.append((kind, payload))
    def set_operation_failure(self, operation, payload): self.failures[operation] = payload
    def clear_operation_failure(self, operation): self.failures.pop(operation, None)
    def operation_failures(self):
        return [{"operation": key, **value} for key, value in self.failures.items()]


def test_ft_pte04_scheduler_observes_cadence_publish_time_backoff_and_recovery():
    class Publisher:
        def __init__(self): self.calls = 0
        def publish(self, end_date):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("vendor unavailable")
            return {"date": end_date}

    engine, publisher, store = Engine(), Publisher(), Store()
    scheduler = RuntimeScheduler(engine, publisher, store)
    for value in (
        datetime(2026, 9, 2, 18, 59, 59), datetime(2026, 9, 2, 19, 0, 0),
        datetime(2026, 9, 2, 19, 0, 4), datetime(2026, 9, 2, 19, 0, 5),
        datetime(2026, 9, 2, 19, 0, 19), datetime(2026, 9, 2, 19, 0, 20),
    ):
        scheduler.tick(value)
    assert publisher.calls == 3
    assert store.values["last_data_publish_date"] == "2026-09-02"
    assert [kind for kind, _ in store.events].count("DATA_PUBLICATION_FAILED") == 1
    assert "SCHEDULER_OPERATION_RECOVERED" in [kind for kind, _ in store.events]
    assert engine.calls.count("orders") >= 3
    assert engine.calls.count("account") == 1

    restored = Store()
    restored.failures["account"] = {
        "fingerprint": "RuntimeError:down", "failure_count": 2,
        "first_at": "2026-09-02T10:00:00", "last_at": "2026-09-02T10:00:05",
        "next_retry": "2026-09-02T10:00:20",
    }
    recovered = Engine()
    scheduler = RuntimeScheduler(recovered, publisher, restored, account_interval=1)
    scheduler.tick(datetime(2026, 9, 2, 10, 0, 19))
    assert "account" not in recovered.calls
    scheduler.tick(datetime(2026, 9, 2, 10, 0, 20))
    assert "account" in recovered.calls
    assert "account" not in restored.failures
    shanghai = timezone(timedelta(hours=8))
    assert is_submission_window(datetime(2026, 9, 2, 9, 30, tzinfo=shanghai))
    assert is_submission_window(datetime(2026, 9, 2, 14, 56, 59, tzinfo=shanghai))
    assert not is_submission_window(datetime(2026, 9, 2, 9, 29, 59, tzinfo=shanghai))
    assert not is_submission_window(datetime(2026, 9, 2, 14, 57, tzinfo=shanghai))
