from datetime import datetime
import json
from pathlib import Path
from threading import Event, Thread
import time
from types import SimpleNamespace

import pytest

from paper_trading_engine.audit import AuditRecorder
from paper_trading_engine.publication_inbox import PublicationInbox, PublicationInboxError
from paper_trading_engine.scheduler import RuntimeScheduler


def test_pte_source_has_no_data_publication_capability():
    source = Path(__file__).resolve().parents[2] / "src/paper_trading_engine"
    assert not (source / "data_publisher.py").exists()
    forbidden = (
        "StrategyDataPublisher",
        "publish_data(",
        "seed_runtime_data",
        "strategy_generation.json",
        "verify_generation",
        "_validation.json",
    )
    for path in source.glob("*.py"):
        content = path.read_text(encoding="utf-8")
        assert not any(token in content for token in forbidden), path.name


class Engine:
    def __init__(self):
        self.calls = []

    def refresh_orders(self):
        self.calls.append("orders")

    def refresh_account(self):
        self.calls.append("account")

    def refresh_decisions(self):
        self.calls.append("decisions")

    def refresh_decision(self, account_id):
        self.calls.append(("decision", account_id))


class Store:
    def __init__(self):
        self.values, self.events, self.failures, self.audit_events = {}, [], {}, []
        self.accounts = []

    def get_setting(self, key):
        return self.values.get(key)

    def set_setting(self, key, value):
        self.values[key] = value

    def add_event(self, kind, payload):
        self.events.append((kind, payload))

    def set_operation_failure(self, operation, payload):
        self.failures[operation] = payload

    def clear_operation_failure(self, operation):
        self.failures.pop(operation, None)

    def operation_failures(self):
        return [{"operation": key, **value} for key, value in self.failures.items()]

    def append_audit_event(self, event):
        value = event.to_dict()
        self.audit_events.append(value)
        return value

    def strategy_virtual_accounts(self):
        return [a for a in self.accounts if a.get("account_type", "STRATEGY") == "STRATEGY"]


def _account(**overrides):
    value = {
        "account_id": "s007-v1",
        "symbol": "588080.SH",
        "asset_type": "etf",
        "status": "RUNNING",
        "strategy_id": "S007",
        "strategy_version": "v1",
        "last_decision_payload": json.dumps({"signal_date": "2026-09-18"}),
    }
    value.update(overrides)
    return value


def _publication(release_id="S007-v1", cutoff="2026-09-18", content="a" * 64):
    return SimpleNamespace(
        release_id=release_id,
        release_hash="b" * 64,
        requested_cutoff=cutoff,
        input_results={
            "bars": SimpleNamespace(
                identity=SimpleNamespace(content_sha256=content)
            )
        },
    )


class Advice:
    def __init__(self, publications=None, error=None):
        self.publications = publications or {"S007-v1": _publication()}
        self.error = error

    def publication_for_account(
        self, *, strategy_id, strategy_version, symbol, asset
    ):
        del symbol, asset
        if self.error is not None:
            raise self.error
        return self.publications[f"{strategy_id}-{strategy_version}"]


def _observed():
    return {
        "data_cutoff": "2026-09-18",
        "instruments": [
            {
                "symbol": "588080.SH",
                "result": {
                    "data_cutoff": "2026-09-18",
                    "publication_id": "PUB-TEST",
                },
            }
        ],
    }


def test_publication_inbox_observes_required_release():
    store = Store()
    store.accounts = [_account()]
    result = PublicationInbox(store=store, advice=Advice()).observe()
    assert len(result["publication_ids"]) == 1
    assert result["instruments"][0]["result"]["publication_id"] == result[
        "publication_ids"
    ][0]
    store.accounts[0]["strategy_version"] = "v2"
    with pytest.raises(PublicationInboxError, match="publication is unavailable"):
        PublicationInbox(store=store, advice=Advice()).observe()


def test_publication_inbox_rejects_srt_publication_failure():
    store = Store()
    store.accounts = [_account()]
    with pytest.raises(PublicationInboxError, match="publication is unavailable"):
        PublicationInbox(
            store=store, advice=Advice(error=RuntimeError("authentication failed"))
        ).observe()


def test_publication_inbox_rejects_different_release_cutoffs():
    store = Store()
    store.accounts = [_account(), _account(account_id="s008-v1", strategy_id="S008")]
    advice = Advice(
        publications={
            "S007-v1": _publication(),
            "S008-v1": _publication("S008-v1", "2026-09-17"),
        }
    )
    with pytest.raises(PublicationInboxError, match="different cutoffs"):
        PublicationInbox(store=store, advice=advice).observe()


def test_scheduler_observes_generation_and_refreshes_once():
    class Inbox:
        def __init__(self):
            self.calls = 0

        def observe(self):
            self.calls += 1
            return _observed()

    store, engine, inbox = Store(), Engine(), Inbox()
    store.accounts = [_account()]
    scheduler = RuntimeScheduler(
        engine, inbox, store, observation_time="20:30", audit=AuditRecorder(store)
    )
    scheduler.tick_daily(datetime(2026, 9, 18, 20, 29, 59))
    assert inbox.calls == 0
    scheduler.tick_daily(datetime(2026, 9, 18, 20, 30, 0))
    assert store.values["last_data_publish_date"] == "2026-09-18"
    assert engine.calls == ["decisions"]
    scheduler.tick_daily(datetime(2026, 9, 18, 20, 30, 5))
    assert engine.calls == ["decisions"]
    assert "MARKET_DATA_OBSERVED" in {e["event_type"] for e in store.audit_events}


def test_scheduler_observation_failure_is_visible_and_retried():
    class Inbox:
        def __init__(self):
            self.calls = 0

        def observe(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("publication incomplete")
            return _observed()

    store, engine, inbox = Store(), Engine(), Inbox()
    store.accounts = [_account()]
    scheduler = RuntimeScheduler(engine, inbox, store, observation_time="00:00")
    scheduler.tick_daily(datetime(2026, 9, 18, 20, 30, 0))
    assert store.values["data_publication_error"] == "publication incomplete"
    assert "publication_observation" in store.failures
    scheduler.tick_daily(datetime(2026, 9, 18, 20, 30, 5))
    assert store.values["last_data_publish_date"] == "2026-09-18"
    assert engine.calls == ["decisions"]


def test_scheduler_onboards_from_existing_publication_without_writing():
    class Inbox:
        def observe(self):
            return _observed()

    store, engine = Store(), Engine()
    store.values.update(
        last_data_publish_date="2026-09-18",
        last_account_decision_date="2026-09-18",
        last_data_publication_ids=json.dumps({"588080.SH": "PUB-TEST"}),
    )
    store.accounts = [_account(last_decision_payload=None)]
    RuntimeScheduler(engine, Inbox(), store, observation_time="00:00").tick_daily(
        datetime(2026, 9, 18, 20, 31)
    )
    assert engine.calls == [("decision", "s007-v1")]


def test_slow_observation_does_not_stop_order_reconciliation():
    entered, release, stopped = Event(), Event(), Event()

    class Inbox:
        def observe(self):
            entered.set()
            assert release.wait(2)
            raise RuntimeError("test stop")

    engine, store = Engine(), Store()
    store.accounts = [_account()]
    scheduler = RuntimeScheduler(
        engine,
        Inbox(),
        store,
        order_interval=0.01,
        account_interval=0.02,
        observation_time="00:00",
    )
    worker = Thread(target=scheduler.run, args=(stopped,))
    worker.start()
    assert entered.wait(1)
    time.sleep(0.65)
    order_calls = engine.calls.count("orders")
    release.set()
    stopped.set()
    worker.join(2)
    assert not worker.is_alive() and order_calls >= 2


def test_scheduler_rejects_malformed_persisted_failure_state():
    store = Store()
    store.failures["orders"] = {
        "fingerprint": "RuntimeError:test",
        "failure_count": "bad",
        "first_at": "2026-09-18T08:00:00",
        "last_at": "2026-09-18T08:00:00",
        "next_retry": "2026-09-18T08:01:00",
    }
    with pytest.raises(ValueError, match="invalid persisted scheduler failure"):
        RuntimeScheduler(Engine(), object(), store)
