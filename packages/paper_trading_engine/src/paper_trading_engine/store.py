"""SQLite-backed runtime state and append-only audit events."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import re
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
            CREATE TABLE IF NOT EXISTS virtual_accounts (
                account_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                baseline_version TEXT NOT NULL,
                baseline_sha256 TEXT NOT NULL,
                initial_cash TEXT NOT NULL,
                cash TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 0,
                average_cost TEXT NOT NULL DEFAULT '0.0000',
                realized_pnl TEXT NOT NULL DEFAULT '0.0000',
                cycle_target INTEGER,
                paused INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS virtual_intents (
                intent_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                decision_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(account_id, decision_id, intent_id)
            );
            CREATE TABLE IF NOT EXISTS virtual_orders (
                order_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                decision_id TEXT NOT NULL,
                valid_session TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                limit_price TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(account_id, decision_id, order_id)
            );
            CREATE TABLE IF NOT EXISTS virtual_fills (
                fill_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                order_id TEXT NOT NULL UNIQUE,
                session TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price TEXT NOT NULL,
                fee TEXT NOT NULL,
                realized_pnl TEXT NOT NULL DEFAULT '0.0000',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS virtual_snapshots (
                account_id TEXT NOT NULL,
                session TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(account_id, session)
            );
            """
        )
        self._ensure_column("virtual_accounts", "average_cost", "TEXT NOT NULL DEFAULT '0.0000'")
        self._ensure_column("virtual_accounts", "realized_pnl", "TEXT NOT NULL DEFAULT '0.0000'")
        self._ensure_column("virtual_fills", "realized_pnl", "TEXT NOT NULL DEFAULT '0.0000'")
        self._connection.commit()

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {row["name"] for row in self._connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

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

    def create_virtual_account(self, account_id, name, baseline_version, baseline_sha256, initial_cash):
        from decimal import Decimal
        cash = Decimal(initial_cash).quantize(Decimal("0.0001"))
        if not cash.is_finite() or cash <= 0:
            raise ValueError("initial cash must be positive")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", str(account_id)) is None:
            raise ValueError("account id must use 1-64 letters, digits, dots, underscores or hyphens")
        if not str(name).strip() or not str(baseline_version).strip():
            raise ValueError("account name and baseline version are required")
        if re.fullmatch(r"[0-9a-f]{64}", str(baseline_sha256).lower()) is None:
            raise ValueError("baseline sha256 must contain 64 hexadecimal characters")
        now = _utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO virtual_accounts(account_id,name,baseline_version,baseline_sha256,initial_cash,cash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (account_id, name, baseline_version, baseline_sha256, str(cash), str(cash), now, now),
            )
        return self.virtual_account(account_id)

    def virtual_account(self, account_id: str):
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM virtual_accounts WHERE account_id=?", (account_id,)
            ).fetchone()
        if row is None:
            raise KeyError(account_id)
        return dict(row)

    def virtual_accounts(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM virtual_accounts ORDER BY created_at, account_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def update_virtual_account(self, account_id: str, *, cash, quantity: int, cycle_target: int | None):
        from decimal import Decimal
        value = str(Decimal(cash).quantize(Decimal("0.0001")))
        if not Decimal(value).is_finite() or Decimal(value) < 0:
            raise ValueError("virtual cash must be finite and non-negative")
        if quantity < 0 or quantity % 100 or (cycle_target is not None and cycle_target % 100):
            raise ValueError("virtual quantities must use non-negative 100-share lots")
        with self._lock, self._connection:
            changed = self._connection.execute(
                "UPDATE virtual_accounts SET cash=?, quantity=?, cycle_target=?, updated_at=? WHERE account_id=?",
                (value, quantity, cycle_target, _utc_now(), account_id),
            ).rowcount
        if not changed:
            raise KeyError(account_id)
        return self.virtual_account(account_id)

    def set_virtual_paused(self, account_id: str, paused: bool):
        with self._lock, self._connection:
            changed = self._connection.execute(
                "UPDATE virtual_accounts SET paused=?, updated_at=? WHERE account_id=?",
                (int(paused), _utc_now(), account_id),
            ).rowcount
        if not changed:
            raise KeyError(account_id)
        return self.virtual_account(account_id)

    def save_virtual_order(self, account_id: str, decision_id: str, valid_session: str, order_id: str, payload: dict[str, object]):
        now = _utc_now()
        with self._lock, self._connection:
            intent_id = f"VI-{account_id}-{decision_id}"
            self._connection.execute(
                "INSERT OR IGNORE INTO virtual_intents(intent_id,account_id,decision_id,payload,created_at) VALUES(?,?,?,?,?)",
                (intent_id, account_id, decision_id, json.dumps(payload, default=str), now),
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO virtual_orders(order_id,account_id,decision_id,valid_session,side,quantity,limit_price,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (order_id, account_id, decision_id, valid_session, payload["side"], int(payload["quantity"]), str(payload["limit_price"]), "PENDING", json.dumps(payload), now, now),
            )

    def virtual_orders(self, account_id: str):
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM virtual_orders WHERE account_id=? ORDER BY created_at,order_id", (account_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def virtual_fills(self, account_id: str):
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM virtual_fills WHERE account_id=? ORDER BY created_at", (account_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def virtual_intents(self, account_id: str):
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM virtual_intents WHERE account_id=? ORDER BY created_at,intent_id", (account_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def settle_virtual_order(self, account_id: str, order_id: str, session: str, price, fee_rate):
        from decimal import Decimal
        with self._lock, self._connection:
            order = self._connection.execute(
                "SELECT * FROM virtual_orders WHERE account_id=? AND order_id=?", (account_id, order_id)
            ).fetchone()
            if order is None:
                raise KeyError(order_id)
            existing = self._connection.execute(
                "SELECT fill_id FROM virtual_fills WHERE order_id=?", (order_id,)
            ).fetchone()
            if existing is not None:
                return False
            account = self._connection.execute(
                "SELECT * FROM virtual_accounts WHERE account_id=?", (account_id,)
            ).fetchone()
            quantity = int(order["quantity"])
            value = Decimal(str(price)) * quantity
            fee = (value * Decimal(str(fee_rate))).quantize(Decimal("0.0001"))
            cash = Decimal(account["cash"])
            held = int(account["quantity"])
            average_cost = Decimal(account["average_cost"])
            realized_total = Decimal(account["realized_pnl"])
            realized_fill = Decimal("0")
            if order["side"] == "BUY":
                cash -= value + fee
                average_cost = ((average_cost * held + value + fee) / (held + quantity)).quantize(Decimal("0.0001"))
                held += quantity
            else:
                if quantity > held:
                    raise ValueError("virtual sell exceeds holdings")
                cash += value - fee
                realized_fill = ((Decimal(str(price)) - average_cost) * quantity - fee).quantize(Decimal("0.0001"))
                realized_total += realized_fill
                held -= quantity
                if held == 0:
                    average_cost = Decimal("0")
            if cash < 0:
                raise ValueError("virtual account has insufficient cash")
            fill_id = f"VF-{order_id}"
            now = _utc_now()
            self._connection.execute(
                "INSERT INTO virtual_fills(fill_id,account_id,order_id,session,side,quantity,price,fee,realized_pnl,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (fill_id, account_id, order_id, session, order["side"], quantity, str(Decimal(str(price)).quantize(Decimal('0.0001'))), str(fee), str(realized_fill), now),
            )
            self._connection.execute(
                "UPDATE virtual_orders SET status='FILLED',updated_at=? WHERE order_id=?", (now, order_id)
            )
            self._connection.execute(
                "UPDATE virtual_accounts SET cash=?,quantity=?,average_cost=?,realized_pnl=?,cycle_target=?,updated_at=? WHERE account_id=?",
                (str(cash.quantize(Decimal('0.0001'))), held, str(average_cost), str(realized_total.quantize(Decimal('0.0001'))), None if held == 0 else account["cycle_target"], now, account_id),
            )
        return True

    def set_virtual_order_status(self, order_id: str, status: str):
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE virtual_orders SET status=?,updated_at=? WHERE order_id=?", (status, _utc_now(), order_id)
            )

    def save_virtual_snapshot(self, account_id: str, session: str, payload: dict[str, object]) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO virtual_snapshots(account_id,session,payload,created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(account_id,session) DO UPDATE SET payload=excluded.payload,created_at=excluded.created_at",
                (account_id, session, json.dumps(payload, ensure_ascii=False, default=str), _utc_now()),
            )

    def virtual_snapshots(self, account_id: str):
        with self._lock:
            rows = self._connection.execute(
                "SELECT session,payload,created_at FROM virtual_snapshots WHERE account_id=? ORDER BY session", (account_id,)
            ).fetchall()
        return [{"session": row["session"], **json.loads(row["payload"]), "created_at": row["created_at"]} for row in rows]

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
