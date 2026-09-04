"""SQLite-backed runtime state and append-only audit events."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import re
from threading import RLock
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from .audit import (
    EVENT_CATALOG,
    AuditCategory,
    AuditEvent,
    AuditOutcome,
    AuditSeverity,
    redact_details,
)


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
                payload TEXT NOT NULL,
                event_id TEXT,
                occurred_at TEXT,
                category TEXT,
                severity TEXT,
                outcome TEXT,
                source TEXT,
                correlation_id TEXT,
                actor_type TEXT,
                actor_id TEXT,
                schema_version TEXT,
                account_id TEXT,
                strategy_id TEXT,
                strategy_version TEXT,
                release_hash TEXT,
                symbol TEXT,
                channel TEXT,
                decision_id TEXT,
                order_id TEXT
            );
            CREATE TABLE IF NOT EXISTS operation_failures (
                operation TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
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
                created_at TEXT NOT NULL,
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
                strategy_id TEXT,
                strategy_name_snapshot TEXT,
                strategy_version TEXT,
                release_hash TEXT,
                qualification_snapshot TEXT,
                symbol TEXT NOT NULL DEFAULT '588080.SH',
                initial_cash TEXT NOT NULL,
                cash TEXT NOT NULL,
                frozen_cash TEXT NOT NULL DEFAULT '0.0000',
                total_assets TEXT NOT NULL DEFAULT '0.0000',
                quantity INTEGER NOT NULL DEFAULT 0,
                average_cost TEXT NOT NULL DEFAULT '0.0000',
                realized_pnl TEXT NOT NULL DEFAULT '0.0000',
                cycle_target INTEGER,
                paused INTEGER NOT NULL DEFAULT 0,
                is_futu_reference INTEGER NOT NULL DEFAULT 0,
                observation_start TEXT,
                last_settlement_session TEXT,
                last_decision_id TEXT,
                last_decision_payload TEXT,
                health TEXT NOT NULL DEFAULT 'READY',
                last_error TEXT,
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
                fill_sequence INTEGER NOT NULL DEFAULT 1,
                source TEXT NOT NULL DEFAULT 'VIRTUAL_MODEL',
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
        self._ensure_column("virtual_accounts", "symbol", "TEXT NOT NULL DEFAULT '588080.SH'")
        self._ensure_column("virtual_accounts", "frozen_cash", "TEXT NOT NULL DEFAULT '0.0000'")
        self._ensure_column("virtual_accounts", "total_assets", "TEXT NOT NULL DEFAULT '0.0000'")
        self._ensure_column("virtual_accounts", "is_futu_reference", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("virtual_accounts", "observation_start", "TEXT")
        self._ensure_column("virtual_accounts", "last_settlement_session", "TEXT")
        self._ensure_column("virtual_accounts", "last_decision_id", "TEXT")
        self._ensure_column("virtual_accounts", "last_decision_payload", "TEXT")
        self._ensure_column("virtual_accounts", "health", "TEXT NOT NULL DEFAULT 'READY'")
        self._ensure_column("virtual_accounts", "last_error", "TEXT")
        self._ensure_column("virtual_accounts", "strategy_id", "TEXT")
        self._ensure_column("virtual_accounts", "strategy_name_snapshot", "TEXT")
        self._ensure_column("virtual_accounts", "strategy_version", "TEXT")
        self._ensure_column("virtual_accounts", "release_hash", "TEXT")
        self._ensure_column("virtual_accounts", "qualification_snapshot", "TEXT")
        self._ensure_column("virtual_fills", "realized_pnl", "TEXT NOT NULL DEFAULT '0.0000'")
        self._ensure_column("virtual_fills", "fill_sequence", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("virtual_fills", "source", "TEXT NOT NULL DEFAULT 'VIRTUAL_MODEL'")
        self._ensure_column("orders", "created_at", "TEXT")
        for column in (
            "event_id", "occurred_at", "category", "severity", "outcome", "source",
            "correlation_id", "actor_type", "actor_id", "schema_version", "account_id",
            "strategy_id", "strategy_version", "release_hash", "symbol", "channel",
            "decision_id", "order_id",
        ):
            self._ensure_column("events", column, "TEXT")
        self._migrate_audit_events()
        self._connection.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_events_event_id ON events(event_id);
            CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events(occurred_at);
            CREATE INDEX IF NOT EXISTS idx_events_category_time ON events(category, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_events_account_time ON events(account_id, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_events_strategy_time
                ON events(strategy_id, strategy_version, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_events_decision ON events(decision_id);
            CREATE INDEX IF NOT EXISTS idx_events_order ON events(order_id);
            """
        )
        self._connection.execute(
            "UPDATE orders SET created_at=updated_at WHERE created_at IS NULL"
        )
        self._connection.execute(
            "UPDATE virtual_accounts SET total_assets=initial_cash WHERE total_assets='0.0000' AND quantity=0"
        )
        self._connection.execute(
            "UPDATE virtual_accounts SET strategy_id='S001',strategy_name_snapshot='综合基线策略',"
            "strategy_version='v1',release_hash=?,qualification_snapshot='PAPER_READY' "
            "WHERE strategy_id IS NULL AND baseline_version='baseline_20260903' AND baseline_sha256=?",
            (
                "ae422915ff736431d70e0381dd6514ee800d861060cc5568712b55c895ddfb62",
                "a7af8864e469b72a94c59eb2e012af5f9a634203cdf5a0214391dd2909e9e331",
            ),
        )
        self._connection.commit()

    @staticmethod
    def _legacy_profile(event_type: str, payload: dict[str, object]):
        aliases = {
            "DATA_PUBLISHED": "MARKET_DATA_PUBLISHED",
            "DATA_PUBLICATION_FAILED": "MARKET_DATA_PUBLICATION_FAILED",
            "FILL_INCREMENT": "ORDER_PARTIALLY_FILLED",
            "PAUSED": "ACCOUNT_PAUSED",
            "RESUMED": "ACCOUNT_RESUMED",
            "PTE_RESTART_REQUESTED": "RESTART_REQUESTED",
            "CHANNEL_INITIALIZATION_FAILED": "DEPENDENCY_DEGRADED",
            "CHANNEL_REFRESH_FAILED": "DEPENDENCY_DEGRADED",
            "VIRTUAL_STARTUP_FAILED": "VIRTUAL_ACCOUNT_FAILED",
        }
        canonical = aliases.get(event_type, event_type)
        details = dict(payload)
        if canonical not in EVENT_CATALOG:
            details = {
                **details,
                "legacy_event_type": event_type,
                "classification_reason": "legacy event type has no audit.v1 mapping",
            }
            canonical = "LEGACY_EVENT"
        category = EVENT_CATALOG[canonical]
        failure = "FAILED" in canonical or canonical == "DEPENDENCY_DEGRADED"
        severity = AuditSeverity.ERROR if failure else (
            AuditSeverity.WARNING if category is AuditCategory.OTHER else AuditSeverity.INFO
        )
        outcome = AuditOutcome.FAILURE if failure else (
            AuditOutcome.UNKNOWN if category is AuditCategory.OTHER else AuditOutcome.SUCCESS
        )
        source = "scheduler" if canonical.startswith(("MARKET_DATA", "SCHEDULER_")) else "engine"
        actor_type = "SCHEDULER" if source == "scheduler" else "ENGINE"
        return canonical, category, severity, outcome, source, actor_type, redact_details(details)

    def _migrate_audit_events(self) -> None:
        rows = self._connection.execute(
            "SELECT * FROM events WHERE event_id IS NULL OR schema_version IS NULL ORDER BY id"
        ).fetchall()
        for row in rows:
            original_payload = json.loads(row["payload"])
            canonical, category, severity, outcome, source, actor_type, details = (
                self._legacy_profile(str(row["event_type"]), original_payload)
            )
            event_id = str(uuid5(
                NAMESPACE_URL,
                f'pte-event:{row["id"]}|{row["created_at"]}|{row["event_type"]}',
            ))
            correlation = str(
                details.get("decision_id") or details.get("channel_order_id") or event_id
            )
            self._connection.execute(
                "UPDATE events SET event_id=?,occurred_at=?,event_type=?,payload=?,category=?,"
                "severity=?,outcome=?,source=?,correlation_id=?,actor_type=?,schema_version=?,"
                "account_id=?,strategy_id=?,strategy_version=?,release_hash=?,symbol=?,channel=?,"
                "decision_id=?,order_id=? WHERE id=?",
                (
                    event_id, row["created_at"], canonical,
                    json.dumps(details, ensure_ascii=False, default=str), category.value,
                    severity.value, outcome.value, source, correlation, actor_type, "audit.v1",
                    details.get("account_id"), details.get("strategy_id"),
                    details.get("strategy_version") or details.get("version"),
                    details.get("release_hash"), details.get("symbol"), details.get("channel"),
                    details.get("decision_id"),
                    details.get("order_id") or details.get("channel_order_id"), row["id"],
                ),
            )
        self._connection.execute(
            "INSERT INTO settings(key,value) VALUES('audit_schema_version','audit.v1') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )

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

    def create_virtual_account(
        self, account_id, name, baseline_version, baseline_sha256, initial_cash,
        *, symbol="588080.SH", is_futu_reference=False, strategy_id=None,
        strategy_name_snapshot=None, strategy_version=None, release_hash=None,
        qualification_snapshot=None,
    ):
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
        strategy_values = (
            strategy_id,
            strategy_name_snapshot,
            strategy_version,
            release_hash,
            qualification_snapshot,
        )
        if any(value is not None for value in strategy_values):
            if any(value is None for value in strategy_values):
                raise ValueError("formal strategy identity must be complete")
            if re.fullmatch(r"S[0-9]{3}", str(strategy_id)) is None:
                raise ValueError("strategy id has invalid format")
            if re.fullmatch(r"v[1-9][0-9]*", str(strategy_version)) is None:
                raise ValueError("strategy version has invalid format")
            if re.fullmatch(r"[0-9a-f]{64}", str(release_hash)) is None:
                raise ValueError("strategy release hash has invalid format")
            if qualification_snapshot not in {"PAPER_READY", "LIVE_READY"}:
                raise ValueError("strategy qualification does not permit paper trading")
        now = _utc_now()
        with self._lock, self._connection:
            if is_futu_reference:
                self._connection.execute("UPDATE virtual_accounts SET is_futu_reference=0")
            self._connection.execute(
                "INSERT INTO virtual_accounts(account_id,name,baseline_version,baseline_sha256,"
                "strategy_id,strategy_name_snapshot,strategy_version,release_hash,"
                "qualification_snapshot,symbol,initial_cash,cash,total_assets,is_futu_reference,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    account_id, name, baseline_version, baseline_sha256, strategy_id,
                    strategy_name_snapshot, strategy_version, release_hash,
                    qualification_snapshot, symbol.upper(), str(cash), str(cash), str(cash),
                    int(is_futu_reference), now, now,
                ),
            )
        return self.virtual_account(account_id)

    def rename_virtual_account(self, old_account_id: str, account_id: str, name: str):
        """Atomically migrate a runtime account identity without losing its ledger."""
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", str(account_id)) is None:
            raise ValueError("account id must use 1-64 letters, digits, dots, underscores or hyphens")
        if not str(name).strip():
            raise ValueError("account name is required")

        def replace_account_id(value):
            if isinstance(value, dict):
                return {
                    key: (account_id if key == "account_id" and item == old_account_id
                          else replace_account_id(item))
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [replace_account_id(item) for item in value]
            return value

        now = _utc_now()
        with self._lock, self._connection:
            source = self._connection.execute(
                "SELECT * FROM virtual_accounts WHERE account_id=?", (old_account_id,)
            ).fetchone()
            if source is None:
                raise KeyError(old_account_id)
            if old_account_id != account_id and self._connection.execute(
                "SELECT 1 FROM virtual_accounts WHERE account_id=?", (account_id,)
            ).fetchone() is not None:
                raise ValueError(f"virtual account already exists: {account_id}")
            if old_account_id == account_id and source["name"] == name:
                return dict(source)

            for table in ("virtual_intents", "virtual_orders", "virtual_fills", "virtual_snapshots"):
                self._connection.execute(
                    f"UPDATE {table} SET account_id=? WHERE account_id=?",
                    (account_id, old_account_id),
                )
            self._connection.execute(
                "UPDATE virtual_accounts SET account_id=?,name=?,updated_at=? WHERE account_id=?",
                (account_id, name, now, old_account_id),
            )

            payload_columns = (
                ("virtual_accounts", "account_id", "last_decision_payload"),
                ("virtual_intents", "intent_id", "payload"),
                ("virtual_orders", "order_id", "payload"),
                ("virtual_snapshots", "rowid", "payload"),
                ("events", "id", "payload"),
            )
            for table, key_column, payload_column in payload_columns:
                rows = self._connection.execute(
                    f"SELECT {key_column}, {payload_column} FROM {table} "
                    f"WHERE {payload_column} IS NOT NULL"
                ).fetchall()
                for row in rows:
                    payload = json.loads(row[payload_column])
                    replaced = replace_account_id(payload)
                    if replaced != payload:
                        self._connection.execute(
                            f"UPDATE {table} SET {payload_column}=? WHERE {key_column}=?",
                            (json.dumps(replaced, ensure_ascii=False, default=str), row[key_column]),
                        )
            event = self._legacy_audit_event(
                "VIRTUAL_ACCOUNT_RENAMED",
                {"old_account_id": old_account_id, "account_id": account_id, "name": name},
                occurred_at=now,
            )
            self._insert_audit_event(event)
        return self.virtual_account(account_id)

    def set_futu_reference(self, account_id: str):
        with self._lock, self._connection:
            if self._connection.execute(
                "SELECT 1 FROM virtual_accounts WHERE account_id=?", (account_id,)
            ).fetchone() is None:
                raise KeyError(account_id)
            self._connection.execute("UPDATE virtual_accounts SET is_futu_reference=0")
            self._connection.execute(
                "UPDATE virtual_accounts SET is_futu_reference=1,updated_at=? WHERE account_id=?",
                (_utc_now(), account_id),
            )
        return self.virtual_account(account_id)

    def migrate_pristine_virtual_account_capital(
        self, account_id: str, *, expected_initial_cash, new_initial_cash,
    ):
        """Change capital only when an account has never entered its trading lifecycle."""
        from decimal import Decimal

        expected = Decimal(expected_initial_cash).quantize(Decimal("0.0001"))
        new_value = Decimal(new_initial_cash).quantize(Decimal("0.0001"))
        if new_value <= 0 or not new_value.is_finite():
            raise ValueError("new initial cash must be positive")
        with self._lock, self._connection:
            account = self._connection.execute(
                "SELECT * FROM virtual_accounts WHERE account_id=?", (account_id,)
            ).fetchone()
            if account is None:
                raise KeyError(account_id)
            current = Decimal(account["initial_cash"])
            if current == new_value:
                return dict(account)
            history_count = sum(
                int(self._connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE account_id=?", (account_id,)
                ).fetchone()[0])
                for table in (
                    "virtual_intents", "virtual_orders", "virtual_fills", "virtual_snapshots",
                )
            )
            pristine = (
                current == expected
                and Decimal(account["cash"]) == expected
                and int(account["quantity"]) == 0
                and Decimal(account["average_cost"]) == 0
                and Decimal(account["realized_pnl"]) == 0
                and history_count == 0
            )
            if not pristine:
                raise ValueError("virtual account has trading history; capital migration refused")
            now = _utc_now()
            self._connection.execute(
                "UPDATE virtual_accounts SET initial_cash=?,cash=?,total_assets=?,cycle_target=NULL,"
                "last_decision_id=NULL,last_decision_payload=NULL,updated_at=? WHERE account_id=?",
                (str(new_value), str(new_value), str(new_value), now, account_id),
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

    def save_virtual_decision(self, account_id: str, decision, cycle_target: int | None):
        if cycle_target is not None and (cycle_target < 0 or cycle_target % 100):
            raise ValueError("cycle target must use non-negative 100-share lots")
        payload = json.dumps(decision, ensure_ascii=False, default=str)
        with self._lock, self._connection:
            changed = self._connection.execute(
                "UPDATE virtual_accounts SET cycle_target=?,last_decision_id=?,last_decision_payload=?,health='OK',last_error=NULL,updated_at=? WHERE account_id=?",
                (cycle_target, decision.get("decision_id"), payload, _utc_now(), account_id),
            ).rowcount
        if not changed:
            raise KeyError(account_id)

    def set_virtual_health(self, account_id: str, health: str, error: str | None = None):
        with self._lock, self._connection:
            changed = self._connection.execute(
                "UPDATE virtual_accounts SET health=?,last_error=?,updated_at=? WHERE account_id=?",
                (health, error, _utc_now(), account_id),
            ).rowcount
        if not changed:
            raise KeyError(account_id)

    def save_virtual_order(self, account_id: str, decision_id: str, valid_session: str, order_id: str, payload: dict[str, object]):
        now = _utc_now()
        with self._lock, self._connection:
            account = self._connection.execute(
                "SELECT * FROM virtual_accounts WHERE account_id=?", (account_id,)
            ).fetchone()
            if account is None:
                raise KeyError(account_id)
            intent_id = f"VI-{account_id}-{decision_id}"
            self._connection.execute(
                "INSERT OR IGNORE INTO virtual_intents(intent_id,account_id,decision_id,payload,created_at) VALUES(?,?,?,?,?)",
                (intent_id, account_id, decision_id, json.dumps(payload, default=str), now),
            )
            inserted = self._connection.execute(
                "INSERT OR IGNORE INTO virtual_orders(order_id,account_id,decision_id,valid_session,side,quantity,limit_price,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (order_id, account_id, decision_id, valid_session, payload["side"], int(payload["quantity"]), str(payload["limit_price"]), "PENDING", json.dumps(payload), now, now),
            ).rowcount
            if inserted:
                scope = {
                    "account_id": account_id, "strategy_id": account["strategy_id"],
                    "strategy_version": account["strategy_version"],
                    "release_hash": account["release_hash"], "symbol": account["symbol"],
                    "channel": "virtual", "decision_id": decision_id, "order_id": order_id,
                }
                self._insert_audit_event(self._new_audit_event(
                    "ORDER_INTENT_CREATED", source="virtual_engine",
                    correlation_id=decision_id, **scope,
                    details={"intent_id": intent_id, **payload},
                ))
                self._insert_audit_event(self._new_audit_event(
                    "ORDER_SUBMITTED", source="virtual_engine",
                    correlation_id=decision_id, **scope,
                    details={"intent_id": intent_id, "channel_status": "PENDING", **payload},
                ))

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

    def settle_virtual_order(
        self, account_id: str, order_id: str, session: str, price, fee_rate,
        diagnostics: dict[str, object] | None = None,
    ):
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
            order_payload = json.loads(order["payload"])
            if diagnostics:
                order_payload.update(diagnostics)
            self._connection.execute(
                "INSERT INTO virtual_fills(fill_id,account_id,order_id,session,side,quantity,price,fee,realized_pnl,fill_sequence,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (fill_id, account_id, order_id, session, order["side"], quantity, str(Decimal(str(price)).quantize(Decimal('0.0001'))), str(fee), str(realized_fill), 1, "VIRTUAL_MODEL", now),
            )
            self._connection.execute(
                "UPDATE virtual_orders SET status='FILLED',payload=?,updated_at=? WHERE order_id=?",
                (json.dumps(order_payload, default=str), now, order_id),
            )
            self._connection.execute(
                "UPDATE virtual_accounts SET cash=?,quantity=?,average_cost=?,realized_pnl=?,cycle_target=?,updated_at=? WHERE account_id=?",
                (str(cash.quantize(Decimal('0.0001'))), held, str(average_cost), str(realized_total.quantize(Decimal('0.0001'))), None if held == 0 else account["cycle_target"], now, account_id),
            )
            self._insert_audit_event(self._new_audit_event(
                "ORDER_FILLED", source="virtual_engine", correlation_id=order["decision_id"],
                account_id=account_id, strategy_id=account["strategy_id"],
                strategy_version=account["strategy_version"],
                release_hash=account["release_hash"], symbol=account["symbol"],
                channel="virtual", decision_id=order["decision_id"], order_id=order_id,
                details={
                    "side": order["side"], "quantity": quantity,
                    "cumulative_quantity": quantity, "average_fill_price": str(price),
                    "fee": str(fee), "session": session,
                },
            ))
        return True

    def set_virtual_order_status(self, order_id: str, status: str, diagnostics: dict[str, object] | None = None):
        with self._lock, self._connection:
            if diagnostics:
                row = self._connection.execute(
                    "SELECT payload FROM virtual_orders WHERE order_id=?", (order_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(order_id)
                payload = json.loads(row["payload"])
                payload.update(diagnostics)
                self._connection.execute(
                    "UPDATE virtual_orders SET status=?,payload=?,updated_at=? WHERE order_id=?",
                    (status, json.dumps(payload, default=str), _utc_now(), order_id),
                )
            else:
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
            self._connection.execute(
                "UPDATE virtual_accounts SET total_assets=?,observation_start=COALESCE(observation_start,?),last_settlement_session=?,health='OK',last_error=NULL,updated_at=? WHERE account_id=?",
                (str(payload["total_assets"]), session, session, _utc_now(), account_id),
            )

    def virtual_snapshots(self, account_id: str):
        with self._lock:
            rows = self._connection.execute(
                "SELECT session,payload,created_at FROM virtual_snapshots WHERE account_id=? ORDER BY session", (account_id,)
            ).fetchall()
        return [{"session": row["session"], **json.loads(row["payload"]), "created_at": row["created_at"]} for row in rows]

    def _legacy_audit_event(
        self, event_type: str, payload: dict[str, object], *, occurred_at: str | None = None,
    ) -> AuditEvent:
        canonical, category, severity, outcome, source, actor_type, details = (
            self._legacy_profile(event_type, payload)
        )
        event_id = str(uuid4())
        decision_id = details.get("decision_id")
        order_id = details.get("order_id") or details.get("channel_order_id")
        return AuditEvent(
            event_id=event_id,
            occurred_at=occurred_at or _utc_now(),
            category=category,
            event_type=canonical,
            severity=severity,
            outcome=outcome,
            source=source,
            correlation_id=str(decision_id or order_id or event_id),
            actor_type=actor_type,
            account_id=details.get("account_id"),
            strategy_id=details.get("strategy_id"),
            strategy_version=details.get("strategy_version") or details.get("version"),
            release_hash=details.get("release_hash"),
            symbol=details.get("symbol"),
            channel=details.get("channel"),
            decision_id=decision_id,
            order_id=order_id,
            details=details,
        )

    def _insert_audit_event(self, event: AuditEvent) -> None:
        self._connection.execute(
            "INSERT INTO events(created_at,event_type,payload,event_id,occurred_at,category,"
            "severity,outcome,source,correlation_id,actor_type,actor_id,schema_version,account_id,"
            "strategy_id,strategy_version,release_hash,symbol,channel,decision_id,order_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.occurred_at, event.event_type,
                json.dumps(event.details, ensure_ascii=False, default=str),
                event.event_id, event.occurred_at, event.category.value, event.severity.value,
                event.outcome.value, event.source, event.correlation_id, event.actor_type,
                event.actor_id, event.schema_version, event.account_id, event.strategy_id,
                event.strategy_version, event.release_hash, event.symbol, event.channel,
                event.decision_id, event.order_id,
            ),
        )

    @staticmethod
    def _new_audit_event(
        event_type: str, *, source: str, correlation_id: str,
        actor_type: str = "ENGINE", account_id: str | None = None,
        strategy_id: str | None = None, strategy_version: str | None = None,
        release_hash: str | None = None, symbol: str | None = None,
        channel: str | None = None, decision_id: str | None = None,
        order_id: str | None = None, details: dict[str, object] | None = None,
    ) -> AuditEvent:
        return AuditEvent(
            event_id=str(uuid4()), occurred_at=_utc_now(),
            category=EVENT_CATALOG[event_type], event_type=event_type,
            severity=AuditSeverity.INFO, outcome=AuditOutcome.SUCCESS,
            source=source, correlation_id=correlation_id, actor_type=actor_type,
            account_id=account_id, strategy_id=strategy_id,
            strategy_version=strategy_version, release_hash=release_hash,
            symbol=symbol, channel=channel, decision_id=decision_id,
            order_id=order_id, details=redact_details(details or {}),
        )

    def append_audit_event(self, event: AuditEvent) -> dict[str, object]:
        with self._lock, self._connection:
            self._insert_audit_event(event)
        return event.to_dict()

    def add_event(self, event_type: str, payload: dict[str, object]) -> None:
        """Compatibility facade for pre-audit callers."""
        event = self._legacy_audit_event(event_type, payload)
        with self._lock, self._connection:
            self._insert_audit_event(event)

    @staticmethod
    def _audit_row(row: sqlite3.Row) -> dict[str, Any]:
        details = json.loads(row["payload"])
        return {
            "id": row["id"],
            "event_id": row["event_id"],
            "occurred_at": row["occurred_at"],
            "created_at": row["occurred_at"],
            "category": row["category"],
            "event_type": row["event_type"],
            "severity": row["severity"],
            "outcome": row["outcome"],
            "source": row["source"],
            "correlation_id": row["correlation_id"],
            "actor_type": row["actor_type"],
            "actor_id": row["actor_id"],
            "schema_version": row["schema_version"],
            "account_id": row["account_id"],
            "strategy_id": row["strategy_id"],
            "strategy_version": row["strategy_version"],
            "release_hash": row["release_hash"],
            "symbol": row["symbol"],
            "channel": row["channel"],
            "decision_id": row["decision_id"],
            "order_id": row["order_id"],
            "details": details,
            "payload": details,
        }

    def query_audit_events(
        self, *, category=None, event_type=None, severity=None, outcome=None,
        account_id=None, strategy_id=None, channel=None, decision_id=None, order_id=None,
        correlation_id=None, before_id=None, limit=50,
    ) -> list[dict[str, Any]]:
        limit = int(limit)
        if not 1 <= limit <= 200:
            raise ValueError("audit event limit must be between 1 and 200")
        filters = {
            "category": category, "event_type": event_type, "severity": severity,
            "outcome": outcome, "account_id": account_id, "strategy_id": strategy_id,
            "channel": channel, "decision_id": decision_id, "order_id": order_id,
            "correlation_id": correlation_id,
        }
        clauses, values = [], []
        for column, value in filters.items():
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(str(value))
        if before_id is not None:
            clauses.append("id<?")
            values.append(int(before_id))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM events{where} ORDER BY id DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [self._audit_row(row) for row in rows]

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.query_audit_events(limit=limit)

    def set_operation_failure(self, operation: str, payload: dict[str, object]) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO operation_failures(operation,payload,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(operation) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                (operation, json.dumps(payload, ensure_ascii=False, default=str), _utc_now()),
            )

    def clear_operation_failure(self, operation: str) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM operation_failures WHERE operation=?", (operation,))

    def operation_failures(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT operation,payload,updated_at FROM operation_failures ORDER BY operation"
            ).fetchall()
        return [
            {"operation": row["operation"], **json.loads(row["payload"]), "updated_at": row["updated_at"]}
            for row in rows
        ]

    def save_intent(self, intent_id: str, decision_id: str, payload: dict[str, object]) -> None:
        now = _utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO intents"
                "(intent_id, decision_id, payload, status, created_at, updated_at) "
                "VALUES(?, ?, ?, 'PENDING_SUBMIT', ?, ?) "
                "ON CONFLICT(intent_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at "
                "WHERE intents.status='PENDING_SUBMIT' AND intents.channel_order_id IS NULL",
                (intent_id, decision_id, json.dumps(payload, default=str), now, now),
            )

    def save_intent_with_event(
        self, intent_id: str, decision_id: str, payload: dict[str, object], event: AuditEvent,
    ) -> None:
        now = _utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO intents(intent_id,decision_id,payload,status,created_at,updated_at) "
                "VALUES(?,?,?,'PENDING_SUBMIT',?,?)",
                (intent_id, decision_id, json.dumps(payload, default=str), now, now),
            )
            self._insert_audit_event(event)

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
                "INSERT INTO orders(channel_order_id, payload, cumulative_filled_quantity, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(channel_order_id) DO UPDATE SET "
                "payload=excluded.payload, cumulative_filled_quantity=excluded.cumulative_filled_quantity, "
                "updated_at=excluded.updated_at",
                (channel_order_id, json.dumps(order, default=str), cumulative, _utc_now(), _utc_now()),
            )
        return cumulative - previous

    def orders(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT o.payload,o.created_at,o.updated_at,i.intent_id,i.decision_id,"
                "i.payload AS intent_payload FROM orders o LEFT JOIN intents i "
                "ON i.channel_order_id=o.channel_order_id ORDER BY o.updated_at DESC"
            ).fetchall()
        result = []
        for row in rows:
            order = json.loads(row["payload"])
            audit = json.loads(row["intent_payload"]) if row["intent_payload"] else {}
            order.update({
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "intent_id": row["intent_id"],
                "decision_id": row["decision_id"],
                "virtual_account_id": audit.get("virtual_account_id"),
                "strategy_release_id": audit.get("strategy_release_id"),
                "release_hash": audit.get("release_hash"),
            })
            result.append(order)
        return result

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
