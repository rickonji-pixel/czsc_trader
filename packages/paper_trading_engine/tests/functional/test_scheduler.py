from datetime import datetime, timedelta, timezone
from subprocess import CompletedProcess

from paper_trading_engine.audit import AuditRecorder
from paper_trading_engine.data_publisher import CliDataPublisher
from paper_trading_engine.scheduler import RuntimeScheduler
from paper_trading_engine.trading_window import is_submission_window


class Engine:
    def __init__(self): self.calls = []
    def refresh_orders(self): self.calls.append("orders")
    def refresh_account(self): self.calls.append("account")
    def refresh_decision_if_changed(self, *, force=False): self.calls.append("decision")


class Store:
    def __init__(self):
        self.values, self.events, self.failures, self.audit_events = {}, [], {}, []
    def get_setting(self, key): return self.values.get(key)
    def set_setting(self, key, value): self.values[key] = value
    def add_event(self, kind, payload): self.events.append((kind, payload))
    def set_operation_failure(self, operation, payload): self.failures[operation] = payload
    def clear_operation_failure(self, operation): self.failures.pop(operation, None)
    def operation_failures(self):
        return [{"operation": key, **value} for key, value in self.failures.items()]
    def append_audit_event(self, event):
        value = event.to_dict()
        self.audit_events.append(value)
        return value


def test_ft_pte04_scheduler_observes_cadence_publish_time_backoff_and_recovery():
    class Publisher:
        def __init__(self): self.calls = 0
        def publish(self, end_date):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("vendor unavailable")
            return {"date": end_date}

    engine, publisher, store = Engine(), Publisher(), Store()
    scheduler = RuntimeScheduler(engine, publisher, store, audit=AuditRecorder(store))
    for value in (
        datetime(2026, 9, 2, 18, 59, 59), datetime(2026, 9, 2, 19, 0, 0),
        datetime(2026, 9, 2, 19, 0, 4), datetime(2026, 9, 2, 19, 0, 5),
        datetime(2026, 9, 2, 19, 0, 19), datetime(2026, 9, 2, 19, 0, 20),
    ):
        scheduler.tick(value)
    assert publisher.calls == 3
    assert store.values["last_data_publish_date"] == "2026-09-02"
    event_types = [event["event_type"] for event in store.audit_events]
    assert event_types.count("SCHEDULER_OPERATION_FAILED") == 1
    assert "SCHEDULER_OPERATION_RECOVERED" in event_types
    assert {event["event_type"] for event in store.audit_events} >= {
        "MARKET_DATA_PUBLICATION_REQUESTED",
        "MARKET_DATA_PUBLICATION_FAILED",
        "MARKET_DATA_PUBLISHED",
    }
    assert {
        event["correlation_id"] for event in store.audit_events
        if event["event_type"].startswith("MARKET_DATA_")
    } == {"publication:2026-09-02"}
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

    cli_store = Store()
    publisher_client = CliDataPublisher(
        executable="czsc-trader", repo_root=".", data_dir=".", symbol="588080.SH",
        asset="etf", start_date="2021-01-01", audit=AuditRecorder(cli_store),
        runner=lambda args, **kwargs: CompletedProcess(
            args, 0, '{"status":"PASS","result":{"data_cutoff":"2026-09-02"}}', ""
        ),
    )
    publisher_client.publish("2026-09-02")
    external = cli_store.audit_events[-1]
    assert external["event_type"] == "EXTERNAL_CALL_SUCCEEDED"
    assert external["details"]["service"] == "trader"
    assert external["details"]["upstream_service"] == "tushare"
    assert external["details"]["operation"] == "data.prepare"
