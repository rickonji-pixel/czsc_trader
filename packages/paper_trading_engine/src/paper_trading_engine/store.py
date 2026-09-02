"""SQLite-backed runtime state and append-only audit events."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PaperStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS intents (
                intent_id TEXT PRIMARY KEY,
                decision_id TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                channel_order_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                channel_order_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                cumulative_filled_quantity INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cancel_tokens (
                token TEXT PRIMARY KEY,
                channel_order_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT
            );
            """
        )
        self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _setting(self, key: str) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_setting(self, key: str) -> str | None:
        with self._lock:
            return self._setting(key)

    def is_paused(self) -> bool:
        with self._lock:
            return self._setting("paused") == "1"

    def set_paused(self, paused: bool) -> None:
        self.set_setting("paused", "1" if paused else "0")

    def mark_reconciled(self) -> None:
        self.set_setting("last_reconcile_at", _utc_now())

    def has_reconciled(self) -> bool:
        with self._lock:
            return self._setting("last_reconcile_at") is not None

    def add_event(self, event_type: str, payload: dict[str, object]) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO events(created_at, event_type, payload) VALUES(?, ?, ?)",
                (_utc_now(), event_type, json.dumps(payload, ensure_ascii=False, default=str)),
            )

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT created_at, event_type, payload FROM events ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [
            {
                "created_at": row["created_at"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload"]),
            }
            for row in rows
        ]

    def save_intent(self, intent_id: str, decision_id: str, payload: dict[str, object]) -> None:
        now = _utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO intents"
                "(intent_id, decision_id, payload, status, created_at, updated_at) "
                "VALUES(?, ?, ?, 'PENDING_SUBMIT', ?, ?)",
                (intent_id, decision_id, json.dumps(payload, default=str), now, now),
            )

    def get_intent(self, intent_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        return self._intent_row(row)

    def find_intent_by_decision(self, decision_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM intents WHERE decision_id = ?", (decision_id,)
            ).fetchone()
        return self._intent_row(row)

    @staticmethod
    def _intent_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "intent_id": row["intent_id"],
            "decision_id": row["decision_id"],
            "payload": json.loads(row["payload"]),
            "status": row["status"],
            "channel_order_id": row["channel_order_id"],
        }

    def bind_intent(self, intent_id: str, channel_order_id: str, status: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE intents SET channel_order_id=?, status=?, updated_at=? WHERE intent_id=?",
                (channel_order_id, status, _utc_now(), intent_id),
            )

    def upsert_order(self, order: dict[str, object]) -> int:
        channel_order_id = str(order["channel_order_id"])
        cumulative = int(order["cumulative_filled_quantity"])
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT cumulative_filled_quantity FROM orders WHERE channel_order_id=?",
                (channel_order_id,),
            ).fetchone()
            previous = 0 if row is None else int(row[0])
            if cumulative < previous:
                raise ValueError("cumulative filled quantity cannot decrease")
            self._connection.execute(
                "INSERT INTO orders(channel_order_id, payload, cumulative_filled_quantity, updated_at) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(channel_order_id) DO UPDATE SET "
                "payload=excluded.payload, cumulative_filled_quantity=excluded.cumulative_filled_quantity, "
                "updated_at=excluded.updated_at",
                (channel_order_id, json.dumps(order, default=str), cumulative, _utc_now()),
            )
        return cumulative - previous

    def orders(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM orders ORDER BY updated_at DESC"
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_snapshot(self, payload: dict[str, object]) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO snapshots(created_at, payload) VALUES(?, ?)",
                (_utc_now(), json.dumps(payload, ensure_ascii=False, default=str)),
            )

    def latest_snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return None if row is None else json.loads(row["payload"])

    def save_cancel_token(self, token: str, channel_order_id: str, expires_at: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO cancel_tokens(token, channel_order_id, expires_at) VALUES(?, ?, ?)",
                (token, channel_order_id, expires_at),
            )

    def consume_cancel_token(self, token: str, channel_order_id: str, now: str) -> str:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT channel_order_id, expires_at, used_at FROM cancel_tokens WHERE token=?",
                (token,),
            ).fetchone()
            if row is None or row["used_at"] is not None or str(row["expires_at"]) < now:
                return "invalid"
            if row["channel_order_id"] != channel_order_id:
                return "bound"
            self._connection.execute(
                "UPDATE cancel_tokens SET used_at=? WHERE token=?", (now, token)
            )
        return "ok"
