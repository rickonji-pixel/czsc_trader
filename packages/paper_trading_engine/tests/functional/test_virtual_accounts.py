import json
import sqlite3
from decimal import Decimal
from uuid import UUID

import pytest

from paper_trading_engine.audit import AuditRecorder

from paper_trading_engine.store import PaperStore
from paper_trading_engine.virtual_fill import settle_limit_order


def test_ft_pte01_virtual_accounts_are_isolated_persistent_and_conservative(tmp_path):
    database = tmp_path / "runtime.db"
    store = PaperStore(database)
    for account_id, version, marker in (("s001-v1", "v1", "a"), ("r1102-v1", "v1", "b")):
        store.create_virtual_account(
            account_id, f"{account_id}模拟账户", "legacy", marker * 64, 100_000,
            strategy_id="S001" if account_id.startswith("s001") else "S002",
            strategy_name_snapshot="策略", strategy_version=version,
            release_hash=marker * 64, qualification_snapshot="PAPER_READY",
        )
    store.save_virtual_order(
        "s001-v1", "DEC-BUY", "2026-09-03", "ORDER-1",
        {"side": "BUY", "quantity": 1000, "limit_price": 1.0, "fee_rate": 0.0005},
    )
    store.settle_virtual_order("s001-v1", "ORDER-1", "2026-09-03", Decimal("1"), Decimal("0.0005"))
    store.set_virtual_paused("r1102-v1", True)
    assert len(store.virtual_fills("s001-v1")) == 1
    assert store.virtual_fills("r1102-v1") == []
    assert store.virtual_account("r1102-v1")["paused"] == 1
    store.close()

    reopened = PaperStore(database)
    assert reopened.virtual_account("s001-v1")["quantity"] == 1000
    assert reopened.virtual_account("r1102-v1")["cash"] == "100000.0000"
    reopened.rename_virtual_account("s001-v1", "s001-forward", "S001-v1模拟账户")
    assert reopened.virtual_orders("s001-v1") == []
    assert reopened.virtual_orders("s001-forward")[0]["order_id"] == "ORDER-1"
    assert settle_limit_order("BUY", 1000, Decimal("1"), Decimal("1.1"), Decimal("1.2"), Decimal("1")).status == "TOUCH_UNCERTAIN"
    assert settle_limit_order("BUY", 1000, Decimal("1"), Decimal("1.1"), Decimal("1.2"), Decimal("0.99")).status == "FILLED"
    reopened.close()

    legacy_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(legacy_path)
    connection.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "created_at TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO events(created_at,event_type,payload) VALUES(?,?,?)",
        (
            "2026-09-03T11:00:00+00:00",
            "DATA_PUBLISHED",
            json.dumps({"account_id": "s001-v1", "date": "2026-09-03"}),
        ),
    )
    connection.execute(
        "INSERT INTO events(created_at,event_type,payload) VALUES(?,?,?)",
        ("2026-09-03T12:00:00+00:00", "MYSTERY", json.dumps({"value": 7})),
    )
    connection.commit()
    connection.close()

    migrated = PaperStore(legacy_path)
    events = migrated.query_audit_events(limit=10)
    assert len(events) == 2
    assert events[1]["event_type"] == "MARKET_DATA_PUBLISHED"
    assert events[1]["category"] == "STRATEGY"
    assert events[1]["details"] == {"account_id": "s001-v1", "date": "2026-09-03"}
    assert events[1]["account_id"] == "s001-v1"
    assert events[0]["event_type"] == "LEGACY_EVENT"
    assert events[0]["details"]["legacy_event_type"] == "MYSTERY"
    assert events[0]["details"]["value"] == 7
    first_ids = [UUID(event["event_id"]) for event in events]
    assert migrated.get_setting("audit_schema_version") == "audit.v1"
    migrated.close()

    migrated = PaperStore(legacy_path)
    assert [UUID(event["event_id"]) for event in migrated.query_audit_events()] == first_ids
    recorder = AuditRecorder(migrated)
    recorder.record(
        "DECISION_GENERATED", source="engine", decision_id="DEC-NEW",
        account_id="s001-v1", strategy_id="S001",
    )
    assert migrated.query_audit_events(strategy_id="S001")[0]["decision_id"] == "DEC-NEW"
    assert migrated.query_audit_events(category="STRATEGY", account_id="s001-v1")
    with pytest.raises(ValueError, match="limit"):
        migrated.query_audit_events(limit=201)
    migrated.close()
