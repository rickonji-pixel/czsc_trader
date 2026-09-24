"""Durable broker observations and shadow-only intents."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any


EXECUTION_MODE = "READ_ONLY_SHADOW"
SCHEMA_VERSION = 1
MAX_BROKER_SNAPSHOTS = 20_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: object) -> str:
    return sha256(_json(value).encode("utf-8")).hexdigest()


class LiveObservationStore:
    """SQLite state that has no representation for an executable live order."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS broker_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    captured_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS broker_orders (
                    order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS broker_executions (
                    trade_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_decisions (
                    source_account_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    PRIMARY KEY(source_account_id, decision_id)
                );
                CREATE TABLE IF NOT EXISTS shadow_intents (
                    source_intent_id TEXT PRIMARY KEY,
                    source_account_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    order_sequence INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    order_type TEXT NOT NULL,
                    reference_price TEXT NOT NULL,
                    valid_session TEXT NOT NULL,
                    source_status TEXT NOT NULL,
                    disposition TEXT NOT NULL CHECK(disposition='SHADOW_ONLY'),
                    payload_sha256 TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    imported_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS import_runs (
                    run_id TEXT PRIMARY KEY,
                    source_database TEXT NOT NULL,
                    source_account_id TEXT NOT NULL,
                    imported_decisions INTEGER NOT NULL,
                    imported_intents INTEGER NOT NULL,
                    completed_at TEXT NOT NULL
                );
                """
            )
            self._connection.execute(
                "INSERT INTO settings(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            self._connection.execute(
                "INSERT INTO settings(key,value) VALUES('execution_mode',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (EXECUTION_MODE,),
            )

    def close(self) -> None:
        self._connection.close()

    def save_snapshot(self, payload: dict[str, object]) -> dict[str, object]:
        if payload.get("order_write_capability") is not False:
            raise ValueError("live observation snapshot must be explicitly read-only")
        snapshot_id = "LBS-" + _digest(payload)[:24].upper()
        captured_at = str(payload["captured_at"])
        symbol = str(payload["symbol"]).upper()
        orders = [*payload.get("today_orders", []), *payload.get("orders", [])]
        executions = [*payload.get("today_executions", []), *payload.get("executions", [])]
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO broker_snapshots(snapshot_id,captured_at,symbol,payload) "
                "VALUES(?,?,?,?)",
                (snapshot_id, captured_at, symbol, _json(payload)),
            )
            for order in orders:
                order_id = str(order.get("order_id") or "")
                if not order_id:
                    raise ValueError("Longbridge order has no order_id")
                encoded = _json(order)
                self._connection.execute(
                    "INSERT INTO broker_orders(order_id,symbol,payload,first_seen_at,last_seen_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET "
                    "payload=excluded.payload,last_seen_at=excluded.last_seen_at",
                    (order_id, str(order.get("symbol") or symbol), encoded, captured_at, captured_at),
                )
            for execution in executions:
                trade_id = str(execution.get("trade_id") or "")
                if not trade_id:
                    raise ValueError("Longbridge execution has no trade_id")
                encoded = _json(execution)
                existing = self._connection.execute(
                    "SELECT payload FROM broker_executions WHERE trade_id=?", (trade_id,),
                ).fetchone()
                if existing is not None and existing["payload"] != encoded:
                    raise ValueError("Longbridge execution changed after first observation")
                self._connection.execute(
                    "INSERT OR IGNORE INTO broker_executions"
                    "(trade_id,order_id,symbol,payload,first_seen_at) VALUES(?,?,?,?,?)",
                    (
                        trade_id, str(execution.get("order_id") or ""),
                        str(execution.get("symbol") or symbol), encoded, captured_at,
                    ),
                )
            self._connection.execute(
                "DELETE FROM broker_snapshots WHERE snapshot_id IN ("
                "SELECT snapshot_id FROM broker_snapshots ORDER BY captured_at DESC "
                "LIMIT -1 OFFSET ?)",
                (MAX_BROKER_SNAPSHOTS,),
            )
        return {
            "snapshot_id": snapshot_id,
            "captured_at": captured_at,
            "symbol": symbol,
            "orders_observed": len(orders),
            "executions_observed": len(executions),
        }

    def save_shadow_decision(
        self, source_account_id: str, decision_id: str, payload: dict[str, object],
    ) -> bool:
        encoded = _json(payload)
        digest = sha256(encoded.encode("utf-8")).hexdigest()
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT payload_sha256 FROM shadow_decisions "
                "WHERE source_account_id=? AND decision_id=?",
                (source_account_id, decision_id),
            ).fetchone()
            if existing is not None:
                if existing["payload_sha256"] != digest:
                    raise ValueError("source decision changed after shadow import")
                return False
            self._connection.execute(
                "INSERT INTO shadow_decisions(source_account_id,decision_id,payload_sha256,"
                "payload,imported_at) VALUES(?,?,?,?,?)",
                (source_account_id, decision_id, digest, encoded, _now()),
            )
        return True

    def save_shadow_intent(self, payload: dict[str, Any]) -> bool:
        source_intent_id = str(payload["source_intent_id"])
        encoded = _json(payload)
        immutable_spec = {
            key: value for key, value in payload.items()
            if key not in {"source_status"}
        }
        digest = _digest(immutable_spec)
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT payload_sha256 FROM shadow_intents WHERE source_intent_id=?",
                (source_intent_id,),
            ).fetchone()
            if existing is not None:
                if existing["payload_sha256"] != digest:
                    raise ValueError("source intent changed after shadow import")
                self._connection.execute(
                    "UPDATE shadow_intents SET source_status=?,payload=?,imported_at=? "
                    "WHERE source_intent_id=?",
                    (str(payload["source_status"]), encoded, _now(), source_intent_id),
                )
                return False
            self._connection.execute(
                "INSERT INTO shadow_intents(source_intent_id,source_account_id,decision_id,"
                "order_sequence,symbol,side,quantity,order_type,reference_price,valid_session,"
                "source_status,disposition,payload_sha256,payload,imported_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'SHADOW_ONLY',?,?,?)",
                (
                    source_intent_id, str(payload["source_account_id"]),
                    str(payload["decision_id"]), int(payload["order_sequence"]),
                    str(payload["symbol"]), str(payload["side"]), int(payload["quantity"]),
                    str(payload["order_type"]), str(payload["reference_price"]),
                    str(payload["valid_session"]), str(payload["source_status"]),
                    digest, encoded, _now(),
                ),
            )
        return True

    def record_import_run(
        self, *, source_database: Path, source_account_id: str,
        imported_decisions: int, imported_intents: int,
    ) -> str:
        completed_at = _now()
        identity = {
            "source_database": str(source_database.resolve()),
            "source_account_id": source_account_id,
            "imported_decisions": imported_decisions,
            "imported_intents": imported_intents,
            "completed_at": completed_at,
        }
        run_id = "LBR-" + _digest(identity)[:24].upper()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO import_runs(run_id,source_database,source_account_id,"
                "imported_decisions,imported_intents,completed_at) VALUES(?,?,?,?,?,?)",
                (
                    run_id, str(source_database.resolve()), source_account_id,
                    imported_decisions, imported_intents, completed_at,
                ),
            )
        return run_id

    def status(self) -> dict[str, object]:
        with self._lock:
            counts = {
                table: int(self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "broker_snapshots", "broker_orders", "broker_executions",
                    "shadow_decisions", "shadow_intents", "import_runs",
                )
            }
            snapshot = self._connection.execute(
                "SELECT snapshot_id,captured_at,symbol FROM broker_snapshots "
                "ORDER BY captured_at DESC LIMIT 1"
            ).fetchone()
            run = self._connection.execute(
                "SELECT * FROM import_runs ORDER BY completed_at DESC LIMIT 1"
            ).fetchone()
        return {
            "schema": "longbridge_live_status.v1",
            "execution_mode": EXECUTION_MODE,
            "order_write_capability": False,
            "live_trading_enabled": False,
            "counts": counts,
            "latest_snapshot": None if snapshot is None else dict(snapshot),
            "latest_shadow_import": None if run is None else dict(run),
        }
