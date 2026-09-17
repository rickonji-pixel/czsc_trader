from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from threading import Event, Thread
import time

import pytest
import pandas as pd
from dataflows import Dataflows, Dataset

from paper_trading_engine.audit import AuditRecorder
from paper_trading_engine.data_publisher import (
    AccountDataPublisher,
    DataPublicationError,
)
from paper_trading_engine.scheduler import RuntimeScheduler
from paper_trading_engine.srt_advice_client import SrtAdviceClient
from paper_trading_engine.trading_window import is_submission_window


def test_account_data_publisher_persists_ready_srt_generation(tmp_path):
    bars = pd.DataFrame(
        {
            "Date": ["2026-09-02"],
            "Open": [6.0],
            "High": [6.1],
            "Low": [5.9],
            "Close": [6.0],
            "Volume": [1000.0],
            "Amount": [6000.0],
        }
    )

    def market(_request):
        return bars.copy(), {
            "vendor": "test",
            "adjustment": "hfq",
            "primary_key": ["Date"],
        }

    def execution(_request):
        return bars.copy(), {
            "vendor": "test",
            "adjustment": "none",
            "primary_key": ["Date"],
        }

    def calendar(request):
        frame = pd.DataFrame(
            {
                "Date": [request.start, "2026-09-03", request.end],
                "IsOpen": [1, 1, 1],
            }
        ).drop_duplicates("Date").sort_values("Date")
        return frame, {"vendor": "test", "primary_key": ["Date"]}

    flows = Dataflows(
        {
            Dataset.ETF_OHLCV.value: market,
            Dataset.ETF_UNADJUSTED_DAILY.value: execution,
            Dataset.TRADING_CALENDAR.value: calendar,
        }
    )
    root = Path(__file__).resolve().parents[4]
    publisher = AccountDataPublisher(
        store=Store(),
        repo_root=root,
        data_dir=tmp_path / "data",
        start_date="2021-01-01",
        dataflows=flows,
    )

    result = publisher.publish_release(
        "510500.SH", "etf", [("S002", "v1")], "2026-09-02"
    )

    assert result["publisher"] == "SRT_DFLS"
    assert result["data_cutoff"] == "2026-09-02"
    assert result["strategy_releases"] == ["S002-v1"]
    assert "srt_s002_v1_publication.json" in result["files"]
    assert (tmp_path / "data/srt_s002_v1_publication.json").is_file()
    assert (tmp_path / "data/510500_manifest.json").is_file()
    assert (tmp_path / "data/510500_execution_manifest.json").is_file()
    decision = SrtAdviceClient(
        repo_root=root,
        data_dir=tmp_path / "data",
        now=lambda: datetime(2026, 9, 2, 20, 31, tzinfo=timezone(timedelta(hours=8))),
    ).get_decision(
        0,
        100000.0,
        strategy_id="S002",
        strategy_version="v1",
        account_id="preflight",
        symbol="510500.SH",
        asset="etf",
    )
    assert decision.strategy["release_id"] == "S002-v1"
    assert decision.data_cutoff.isoformat() == "2026-09-02"


class Engine:
    def __init__(self): self.calls = []
    def refresh_orders(self): self.calls.append("orders")
    def refresh_account(self): self.calls.append("account")
    def refresh_decisions(self): self.calls.append("decisions")
    def refresh_decision(self, account_id): self.calls.append(("decision", account_id))


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
    def strategy_virtual_accounts(self): return list(self.accounts)


def test_ft_pte04_scheduler_observes_cadence_publish_time_backoff_and_recovery(tmp_path):
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
    cli_store.accounts = [
        {
            "symbol": "588080.SH", "asset_type": "etf", "status": "RUNNING",
            "strategy_id": "S001", "strategy_version": "v1",
        },
        {
            "symbol": "510500.SH", "asset_type": "etf", "status": "RUNNING",
            "strategy_id": "S003", "strategy_version": "v1",
        },
    ]
    multi = AccountDataPublisher(
        store=cli_store,
        repo_root=".",
        data_dir=".",
        start_date="2021-01-01",
        audit=AuditRecorder(cli_store),
    )
    calls: list[tuple[str, str, tuple[tuple[str, str], ...], str]] = []
    def publish_release(symbol, asset, releases, end_date):
        calls.append((symbol, asset, tuple(releases), end_date))
        return {"data_cutoff": end_date, "generation_id": "GEN-TEST"}
    multi.publish_release = publish_release
    multi_result = multi.publish("2026-09-02")
    published_symbols = [item[0] for item in calls]
    assert published_symbols == ["510500.SH", "588080.SH"]
    assert [item["symbol"] for item in multi_result["instruments"]] == published_symbols
    assert {item[2][0] for item in calls} == {("S001", "v1"), ("S003", "v1")}
    assert multi_result["generation_ids"] == ["GEN-TEST", "GEN-TEST"]

    failed_runtime = AccountDataPublisher(
        store=cli_store,
        repo_root=".",
        data_dir=tmp_path / "failed-data",
        start_date="2021-01-01",
    )
    failed_runtime._release = lambda *_args: (_ for _ in ()).throw(
        DataPublicationError("runtime support unavailable")
    )
    with pytest.raises(DataPublicationError, match="runtime support unavailable"):
        failed_runtime.publish_release(
            "588080.SH", "etf", [("S007", "v1")], "2026-09-15"
        )
    assert not (tmp_path / "failed-data").exists()

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


def test_ft_pte04_account_created_after_daily_publication_is_onboarded():
    class Publisher:
        def __init__(self): self.calls = []
        def publish(self, cutoff):
            self.calls.append(cutoff)
            return {"data_cutoff": cutoff, "generation_ids": ["GEN-TEST"]}

    store = Store()
    store.values.update(
        last_data_publish_date="2026-09-11",
        last_account_decision_date="2026-09-11",
        last_data_publish_attempt_date="2026-09-11",
    )
    store.accounts = [{
        "account_id": "s003-v1",
        "strategy_id": "S003",
        "strategy_version": "v1",
        "status": "RUNNING",
        "last_decision_payload": None,
    }]
    engine, publisher = Engine(), Publisher()
    scheduler = RuntimeScheduler(engine, publisher, store)

    scheduler.tick_daily(datetime(2026, 9, 11, 20, 45))

    assert publisher.calls == ["2026-09-11"]
    assert ("decision", "s003-v1") in engine.calls

    store.accounts[0]["last_decision_payload"] = json.dumps(
        {"signal_date": "2026-09-11"}
    )
    scheduler.tick_daily(datetime(2026, 9, 11, 20, 46))
    assert publisher.calls == ["2026-09-11"]


def test_ft_pte04_slow_daily_publication_does_not_stop_order_reconciliation():
    entered, release, stopped = Event(), Event(), Event()

    class SlowPublisher:
        def publish(self, end_date):
            entered.set()
            assert release.wait(2)
            return {"data_cutoff": end_date}

    engine, store = Engine(), Store()
    scheduler = RuntimeScheduler(
        engine, SlowPublisher(), store,
        order_interval=0.01, account_interval=0.02, publish_time="00:00",
    )
    worker = Thread(target=scheduler.run, args=(stopped,))
    worker.start()
    assert entered.wait(1)
    time.sleep(0.65)
    order_calls_while_publication_blocked = engine.calls.count("orders")
    release.set()
    stopped.set()
    worker.join(2)
    assert not worker.is_alive()
    assert order_calls_while_publication_blocked >= 2
    assert store.get_setting("scheduler_heartbeat_at") is not None
