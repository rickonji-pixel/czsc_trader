from datetime import datetime, timedelta, timezone
from subprocess import CompletedProcess

from paper_trading_engine.audit import AuditRecorder
from paper_trading_engine.data_publisher import AccountDataPublisher, CliDataPublisher
from paper_trading_engine.scheduler import RuntimeScheduler
from paper_trading_engine.trading_window import is_submission_window


class Engine:
    def __init__(self): self.calls = []
    def refresh_orders(self): self.calls.append("orders")
    def refresh_account(self): self.calls.append("account")
    def refresh_decisions(self): self.calls.append("decisions")


class Store:
    def __init__(self):
        self.values, self.events, self.failures, self.audit_events = {}, [], {}, []
        self.accounts = []
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
    def virtual_accounts(self): return list(self.accounts)


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
    scheduler.tick(datetime(2026, 9, 2, 20, 29, 59))
    assert publisher.calls == 0
    for value in (
        datetime(2026, 9, 2, 20, 30, 0),
        datetime(2026, 9, 2, 20, 30, 4), datetime(2026, 9, 2, 20, 30, 5),
        datetime(2026, 9, 2, 20, 30, 19), datetime(2026, 9, 2, 20, 30, 20),
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
    assert engine.calls.count("decisions") == 1

    catchup_store = Store()
    catchup_store.values["last_data_publish_date"] = "2026-09-02"
    catchup_engine = Engine()
    catchup = RuntimeScheduler(catchup_engine, publisher, catchup_store)
    catchup.tick(datetime(2026, 9, 3, 10, 0, 0))
    catchup.tick(datetime(2026, 9, 3, 10, 0, 5))
    assert catchup_engine.calls.count("decisions") == 1
    assert catchup_store.values["last_account_decision_date"] == "2026-09-02"

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

    calls: list[list[str]] = []
    def multi_runner(args, **kwargs):
        calls.append(args)
        return CompletedProcess(
            args, 0, '{"status":"PASS","result":{"data_cutoff":"2026-09-02"}}', ""
        )

    cli_store.accounts = [
        {"symbol": "588080.SH", "asset_type": "etf", "status": "RUNNING"},
        {"symbol": "510500.SH", "asset_type": "etf", "status": "RUNNING"},
    ]
    multi = AccountDataPublisher(
        store=cli_store,
        executable="czsc-trader",
        repo_root=".",
        data_dir=".",
        start_date="2021-01-01",
        runner=multi_runner,
        audit=AuditRecorder(cli_store),
    )
    multi_result = multi.publish("2026-09-02")
    published_symbols = [
        args[args.index("--symbol") + 1]
        for args in calls
    ]
    assert published_symbols == ["510500.SH", "588080.SH"]
    assert [item["symbol"] for item in multi_result["instruments"]] == published_symbols


def test_ft_pte04_failed_account_batch_is_not_marked_complete():
    class RetryingEngine(Engine):
        def __init__(self):
            super().__init__()
            self.decision_attempts = 0

        def refresh_decisions(self):
            self.decision_attempts += 1
            if self.decision_attempts == 1:
                raise RuntimeError("one account failed")
            self.calls.append("decisions")

    store = Store()
    store.values["last_data_publish_date"] = "2026-09-02"
    engine = RetryingEngine()
    scheduler = RuntimeScheduler(engine, object(), store)
    scheduler.tick(datetime(2026, 9, 3, 10, 0, 0))
    assert store.get_setting("last_account_decision_date") is None
    scheduler.tick(datetime(2026, 9, 3, 10, 0, 5))
    assert store.get_setting("last_account_decision_date") == "2026-09-02"
