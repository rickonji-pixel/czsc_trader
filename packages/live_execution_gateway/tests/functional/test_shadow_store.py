import json
from pathlib import Path
import sqlite3

import pytest

import live_execution_gateway.store as store_module
from live_execution_gateway.shadow import ShadowImportError, import_pte_shadow
from live_execution_gateway.store import LiveObservationStore


def _source_database(path: Path, *, channel_id: str = "futu_simulate_us") -> None:
    connection = sqlite3.connect(path)
    with connection:
        connection.executescript(
            """
            CREATE TABLE virtual_accounts (
                account_id TEXT PRIMARY KEY,
                account_type TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                qualification_snapshot TEXT NOT NULL
            );
            CREATE TABLE decisions (
                account_id TEXT NOT NULL,
                decision_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                signal_date TEXT NOT NULL
            );
            CREATE TABLE intents (
                intent_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                decision_id TEXT NOT NULL,
                order_sequence INTEGER NOT NULL,
                channel_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                limit_price TEXT NOT NULL,
                valid_session TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO virtual_accounts VALUES(?,?,?,?,?,?)",
            ("s103-v1-us", "STRATEGY", channel_id, "MU.US", "stock", "PAPER_READY"),
        )
        decision = {
            "decision_id": "DEC-1", "symbol": "MU.US", "signal_date": "2026-09-21",
            "valid_session": "2026-09-22", "action": "BUY",
        }
        connection.execute(
            "INSERT INTO decisions VALUES(?,?,?,?)",
            ("s103-v1-us", "DEC-1", json.dumps(decision), "2026-09-21"),
        )
        intent = {"order_type": "MARKET"}
        connection.execute(
            "INSERT INTO intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "PTE-1", "s103-v1-us", "DEC-1", 0, channel_id, "MU.US", "BUY", 93,
                "1043.9600", "2026-09-22", json.dumps(intent), "FILLED_ALL",
            ),
        )
    connection.close()


def test_shadow_import_is_isolated_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "paper.db"
    destination = tmp_path / "live-shadow.db"
    _source_database(source)
    source_before = source.read_bytes()
    store = LiveObservationStore(destination)
    try:
        first = import_pte_shadow(
            store, source_database=source, source_account_id="s103-v1-us", symbol="MU.US",
        )
        second = import_pte_shadow(
            store, source_database=source, source_account_id="s103-v1-us", symbol="MU.US",
        )
        status = store.status()
    finally:
        store.close()

    assert first["decisions_imported"] == 1
    assert first["intents_imported"] == 1
    assert first["disposition"] == "SHADOW_ONLY"
    assert second["decisions_imported"] == 0
    assert second["intents_imported"] == 0
    assert status["execution_mode"] == "READ_ONLY_SHADOW"
    assert status["order_write_capability"] is False
    assert status["live_trading_enabled"] is False
    assert status["counts"]["shadow_intents"] == 1
    assert source.read_bytes() == source_before


def test_shadow_import_rejects_non_us_simulation_source(tmp_path: Path) -> None:
    source = tmp_path / "paper.db"
    _source_database(source, channel_id="futu_simulate_cn")
    store = LiveObservationStore(tmp_path / "live-shadow.db")
    try:
        with pytest.raises(ShadowImportError, match="outside the approved"):
            import_pte_shadow(
                store, source_database=source, source_account_id="s103-v1-us", symbol="MU.US",
            )
    finally:
        store.close()


def test_broker_execution_is_immutable(tmp_path: Path) -> None:
    store = LiveObservationStore(tmp_path / "live-shadow.db")
    base = {
        "captured_at": "2026-09-22T00:00:00+00:00",
        "symbol": "MU.US",
        "order_write_capability": False,
        "today_orders": [],
        "today_executions": [{
            "trade_id": "T1", "order_id": "O1", "symbol": "MU.US", "quantity": "1",
        }],
    }
    try:
        store.save_snapshot(base)
        changed = json.loads(json.dumps(base))
        changed["captured_at"] = "2026-09-22T00:01:00+00:00"
        changed["today_executions"][0]["quantity"] = "2"
        with pytest.raises(ValueError, match="changed after first observation"):
            store.save_snapshot(changed)
    finally:
        store.close()


def test_snapshot_retention_does_not_delete_order_or_execution_facts(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(store_module, "MAX_BROKER_SNAPSHOTS", 2)
    store = LiveObservationStore(tmp_path / "live-shadow.db")
    try:
        for minute in range(3):
            store.save_snapshot({
                "captured_at": f"2026-09-22T00:0{minute}:00+00:00",
                "symbol": "MU.US", "order_write_capability": False,
                "today_orders": [{
                    "order_id": "O1", "symbol": "MU.US", "status": "Filled",
                }],
                "today_executions": [{
                    "trade_id": "T1", "order_id": "O1", "symbol": "MU.US",
                    "quantity": "1",
                }],
            })
        status = store.status()
    finally:
        store.close()

    assert status["counts"]["broker_snapshots"] == 2
    assert status["counts"]["broker_orders"] == 1
    assert status["counts"]["broker_executions"] == 1
