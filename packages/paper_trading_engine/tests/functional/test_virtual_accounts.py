import json
import sqlite3

import pytest

from paper_trading_engine.store import PaperStore


def create_account(store, account_id, version, marker):
    return store.create_virtual_account(
        account_id, f"{account_id}模拟账户", "legacy", marker * 64, 100_000,
        strategy_id="S001", strategy_name_snapshot="综合基线策略",
        strategy_version=version, release_hash=marker * 64,
        qualification_snapshot="PAPER_READY",
    )


def test_ft_pte01_account_model_migration_and_independent_futu_ledgers(tmp_path):
    store = PaperStore(tmp_path / "account-centric.db")
    create_account(store, "s001-v1", "v1", "a")
    create_account(store, "s001-v2", "v2", "b")
    assert {row["channel_id"] for row in store.virtual_accounts()} == {"futu"}
    assert len(store.query_audit_events(event_type="ACCOUNT_STRATEGY_BOUND")) == 2
    assert len(store.query_audit_events(event_type="ACCOUNT_CHANNEL_BOUND", channel="futu")) == 2
    tables = {row[0] for row in store._connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert not {"virtual_intents", "virtual_orders", "virtual_fills", "virtual_snapshots"} & tables

    intent = store.create_account_intent(
        account_id="s001-v2", decision_id="DEC-2", order_sequence=0,
        symbol="588080.SH", side="BUY", quantity=1000,
        limit_price="1.680", valid_session="2026-09-04", fee_rate="0.0005",
    )
    store.bind_channel_order(intent["intent_id"], "1001", {
        "channel_order_id": "1001", "symbol": "588080.SH", "side": "BUY",
        "quantity": 1000, "limit_price": 1.68, "status": "SUBMITTED",
        "cumulative_filled_quantity": 0, "average_fill_price": 0,
        "remark": intent["intent_id"],
    })
    store.apply_fill_increment(
        "1001", cumulative_quantity=1000, average_price="1.670",
        occurred_at="2026-09-04T01:31:00+00:00",
    )
    assert store.virtual_account("s001-v1")["quantity"] == 0
    assert store.virtual_account("s001-v2")["quantity"] == 1000
    assert store.virtual_account("s001-v2")["cash"] == "98329.1650"
    assert store.account_fills("s001-v2")[0]["channel_id"] == "futu"
    store.close()

    legacy = tmp_path / "unsafe-legacy.db"
    connection = sqlite3.connect(legacy)
    connection.executescript(
        "CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
        "CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,event_type TEXT NOT NULL,payload TEXT NOT NULL);"
        "CREATE TABLE intents(intent_id TEXT PRIMARY KEY,decision_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,status TEXT NOT NULL,channel_order_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);"
        "CREATE TABLE orders(channel_order_id TEXT PRIMARY KEY,payload TEXT NOT NULL,cumulative_filled_quantity INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);"
    )
    connection.execute(
        "INSERT INTO intents VALUES(?,?,?,?,?,?,?)",
        ("OLD", "DEC", json.dumps({}), "PENDING_SUBMIT", None, "x", "x"),
    )
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="requires empty legacy trading tables"):
        PaperStore(legacy)

    safe_legacy = tmp_path / "safe-legacy.db"
    connection = sqlite3.connect(safe_legacy)
    connection.executescript(
        "CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
        "CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,event_type TEXT NOT NULL,payload TEXT NOT NULL);"
        "CREATE TABLE intents(intent_id TEXT PRIMARY KEY,decision_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,status TEXT NOT NULL,channel_order_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);"
        "CREATE TABLE orders(channel_order_id TEXT PRIMARY KEY,payload TEXT NOT NULL,cumulative_filled_quantity INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);"
    )
    connection.close()
    migrated = PaperStore(safe_legacy)
    assert migrated.get_setting("account_execution_schema") == "account_execution.v1"
    assert migrated.query_audit_events(event_type="ACCOUNT_EXECUTION_MIGRATED")
    migrated.close()
