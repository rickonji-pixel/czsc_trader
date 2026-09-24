"""Durable live-deployment, decision, intent, and arming state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import secrets
import sqlite3
from threading import RLock
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None = None) -> str:
    return (moment or _now()).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: object) -> str:
    return sha256(_json(value).encode("utf-8")).hexdigest()


class LiveTradeStore:
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
                CREATE TABLE IF NOT EXISTS live_deployments (
                    account_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    release_hash TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    asset_type TEXT NOT NULL CHECK(asset_type='stock'),
                    channel_id TEXT NOT NULL CHECK(channel_id='longbridge_live_us'),
                    max_order_notional TEXT NOT NULL,
                    max_gross_notional TEXT NOT NULL,
                    cash_reserve TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('DISARMED','ARMED','BLOCKED')),
                    armed_until TEXT,
                    arm_generation INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS arm_challenges (
                    challenge_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    confirmation_phrase TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS live_decisions (
                    account_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    source_decision_id TEXT NOT NULL,
                    signal_date TEXT NOT NULL,
                    valid_session TEXT NOT NULL,
                    qualification TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(account_id,decision_id),
                    UNIQUE(account_id,source_decision_id)
                );
                CREATE TABLE IF NOT EXISTS live_intents (
                    intent_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    order_sequence INTEGER NOT NULL,
                    arm_generation INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    order_type TEXT NOT NULL,
                    outside_rth TEXT NOT NULL DEFAULT 'RTH_ONLY',
                    reference_price TEXT NOT NULL,
                    time_in_force TEXT NOT NULL,
                    valid_session TEXT NOT NULL,
                    client_request_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    broker_order_id TEXT UNIQUE,
                    submitted_at TEXT,
                    last_error TEXT,
                    payload_sha256 TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account_id,decision_id,order_sequence)
                );
                CREATE TABLE IF NOT EXISTS live_events (
                    event_id TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    account_id TEXT,
                    intent_id TEXT,
                    outcome TEXT NOT NULL,
                    details TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"] for row in self._connection.execute("PRAGMA table_info(live_intents)")
            }
            if "outside_rth" not in columns:
                self._connection.execute(
                    "ALTER TABLE live_intents ADD COLUMN outside_rth TEXT NOT NULL "
                    "DEFAULT 'RTH_ONLY'"
                )

    def close(self) -> None:
        self._connection.close()

    def event(
        self, event_type: str, *, account_id: str | None = None,
        intent_id: str | None = None, outcome: str = "SUCCESS",
        details: dict[str, object] | None = None,
    ) -> str:
        occurred_at = _iso()
        body = details or {}
        event_id = "LBE-" + sha256(
            f"{event_type}\0{account_id}\0{intent_id}\0{occurred_at}\0{_json(body)}".encode()
        ).hexdigest()[:24].upper()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO live_events VALUES(?,?,?,?,?,?,?)",
                (event_id, occurred_at, event_type, account_id, intent_id, outcome, _json(body)),
            )
        return event_id

    def register_deployment(
        self, *, account_id: str, strategy_id: str, strategy_version: str,
        release_hash: str, symbol: str, max_order_notional: Decimal,
        max_gross_notional: Decimal, cash_reserve: Decimal,
    ) -> dict[str, Any]:
        limits = tuple(Decimal(value) for value in (
            max_order_notional, max_gross_notional, cash_reserve,
        ))
        if limits[0] <= 0 or limits[1] <= 0 or limits[0] > limits[1] or limits[2] < 0:
            raise ValueError("live risk limits are invalid")
        now = _iso()
        identity = {
            "account_id": account_id,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "release_hash": release_hash,
            "symbol": symbol.upper(),
            "asset_type": "stock",
            "channel_id": "longbridge_live_us",
        }
        initial_limits = {
            "max_order_notional": str(limits[0]),
            "max_gross_notional": str(limits[1]),
            "cash_reserve": str(limits[2]),
        }
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT * FROM live_deployments WHERE account_id=?", (account_id,),
            ).fetchone()
            if existing is not None:
                actual = {key: str(existing[key]) for key in identity}
                expected = {key: str(value) for key, value in identity.items()}
                if actual != expected:
                    raise ValueError("live deployment immutable identity differs")
                return dict(existing)
            self._connection.execute(
                "INSERT INTO live_deployments(account_id,strategy_id,strategy_version,release_hash,"
                "symbol,asset_type,channel_id,max_order_notional,max_gross_notional,cash_reserve,"
                "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'DISARMED',?,?)",
                (*identity.values(), *initial_limits.values(), now, now),
            )
        self.event(
            "LIVE_DEPLOYMENT_REGISTERED",
            account_id=account_id,
            details={**identity, **initial_limits},
        )
        return self.deployment(account_id)

    def deployment(self, account_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM live_deployments WHERE account_id=?", (account_id,),
            ).fetchone()
        if row is None:
            raise KeyError(account_id)
        return dict(row)

    def update_risk_limits(
        self, account_id: str, *, max_order_notional: Decimal,
        max_gross_notional: Decimal, cash_reserve: Decimal,
    ) -> dict[str, Any]:
        values = tuple(Decimal(value) for value in (
            max_order_notional, max_gross_notional, cash_reserve,
        ))
        if values[0] <= 0 or values[1] <= 0 or values[0] > values[1] or values[2] < 0:
            raise ValueError("live risk limits are invalid")
        deployment = self.deployment(account_id)
        if deployment["status"] != "DISARMED":
            raise PermissionError("risk limits can change only while explicitly DISARMED")
        if self.ready_intents(account_id) or self.unresolved_intents(account_id):
            raise PermissionError("risk limits cannot change with executable or unresolved intents")
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE live_deployments SET max_order_notional=?,max_gross_notional=?,"
                "cash_reserve=?,updated_at=? WHERE account_id=?",
                (str(values[0]), str(values[1]), str(values[2]), _iso(), account_id),
            )
        self.event("LIVE_RISK_LIMITS_UPDATED", account_id=account_id, details={
            "max_order_notional": str(values[0]),
            "max_gross_notional": str(values[1]),
            "cash_reserve": str(values[2]),
        })
        return self.deployment(account_id)

    def issue_arm_challenge(self, account_id: str, *, minutes: int = 5) -> dict[str, str]:
        deployment = self.deployment(account_id)
        if deployment["status"] != "DISARMED":
            raise PermissionError("live deployment must be DISARMED before requesting authorization")
        if self.ready_intents(account_id) or self.unresolved_intents(account_id):
            raise PermissionError("live deployment has executable or unresolved intents")
        if not 1 <= minutes <= 10:
            raise ValueError("arm challenge lifetime must be 1-10 minutes")
        challenge_id = "LBC-" + secrets.token_hex(12).upper()
        phrase = "ENABLE_LONG_BRIDGE_REAL_MONEY_" + challenge_id[-6:]
        expires = _now() + timedelta(minutes=minutes)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO arm_challenges VALUES(?,?,?,?,NULL)",
                (challenge_id, account_id, phrase, _iso(expires)),
            )
        self.event("LIVE_ARM_CHALLENGE_ISSUED", account_id=account_id, details={
            "challenge_id": challenge_id, "expires_at": _iso(expires),
        })
        return {
            "challenge_id": challenge_id,
            "confirmation_phrase": phrase,
            "expires_at": _iso(expires),
        }

    def arm(
        self, account_id: str, *, challenge_id: str, confirmation_phrase: str,
        hours: int, actor: str, reason: str,
    ) -> dict[str, Any]:
        if not 1 <= hours <= 24 or not actor.strip() or not reason.strip():
            raise ValueError("arming requires 1-24 hours, actor, and reason")
        now = _now()
        with self._lock, self._connection:
            challenge = self._connection.execute(
                "SELECT * FROM arm_challenges WHERE challenge_id=? AND account_id=?",
                (challenge_id, account_id),
            ).fetchone()
            if challenge is None or challenge["used_at"] is not None:
                raise ValueError("live arm challenge is missing or already used")
            if datetime.fromisoformat(challenge["expires_at"]) < now:
                raise ValueError("live arm challenge expired")
            if not secrets.compare_digest(challenge["confirmation_phrase"], confirmation_phrase):
                raise ValueError("live arm confirmation phrase differs")
            deployment = self._connection.execute(
                "SELECT * FROM live_deployments WHERE account_id=?", (account_id,),
            ).fetchone()
            if deployment is None:
                raise KeyError(account_id)
            if deployment["status"] != "DISARMED":
                raise PermissionError("live deployment must be DISARMED before arming")
            unresolved = self._connection.execute(
                "SELECT COUNT(*) FROM live_intents WHERE account_id=? AND status IN "
                "('READY','SUBMITTING','SUBMISSION_UNCERTAIN','SUBMITTED','PARTIAL_FILLED',"
                "'CANCEL_PENDING','CANCEL_UNCERTAIN','REPLACE_PENDING')",
                (account_id,),
            ).fetchone()[0]
            if unresolved:
                raise PermissionError("live deployment has executable or unresolved intents")
            generation = int(deployment["arm_generation"]) + 1
            until = now + timedelta(hours=hours)
            self._connection.execute(
                "UPDATE arm_challenges SET used_at=? WHERE challenge_id=?",
                (_iso(now), challenge_id),
            )
            self._connection.execute(
                "UPDATE live_deployments SET status='ARMED',armed_until=?,arm_generation=?,"
                "last_error=NULL,updated_at=? WHERE account_id=?",
                (_iso(until), generation, _iso(now), account_id),
            )
        self.event("LIVE_TRADING_ARMED", account_id=account_id, details={
            "actor": actor, "reason": reason, "armed_until": _iso(until),
            "arm_generation": generation,
        })
        return self.deployment(account_id)

    def disarm(self, account_id: str, *, reason: str, blocked: bool = False) -> dict[str, Any]:
        status = "BLOCKED" if blocked else "DISARMED"
        with self._lock, self._connection:
            updated = self._connection.execute(
                "UPDATE live_deployments SET status=?,armed_until=NULL,last_error=?,updated_at=? "
                "WHERE account_id=?",
                (status, reason, _iso(), account_id),
            ).rowcount
            if not updated:
                raise KeyError(account_id)
            self._connection.execute(
                "UPDATE live_intents SET status='INVALIDATED_BY_DISARM',last_error=?,updated_at=? "
                "WHERE account_id=? AND status='READY'",
                (reason, _iso(), account_id),
            )
        self.event(
            "LIVE_TRADING_BLOCKED" if blocked else "LIVE_TRADING_DISARMED",
            account_id=account_id,
            outcome="FAILURE" if blocked else "SUCCESS",
            details={"reason": reason},
        )
        return self.deployment(account_id)

    def armed(self, account_id: str) -> bool:
        deployment = self.deployment(account_id)
        if deployment["status"] != "ARMED" or not deployment["armed_until"]:
            return False
        if datetime.fromisoformat(deployment["armed_until"]) <= _now():
            self.disarm(account_id, reason="实盘授权已到期")
            return False
        return True

    def save_decision_and_intents(
        self, *, account_id: str, decision: dict[str, Any], qualification: str,
        executable: bool,
    ) -> dict[str, object]:
        source_id = str(decision.get("source_decision_id") or decision["decision_id"])
        decision_id = "LBD-" + sha256(f"{account_id}\0{source_id}".encode()).hexdigest()[:24].upper()
        disposition = "READY" if executable else "SHADOW_ONLY"
        payload = {**decision, "decision_id": decision_id, "source_decision_id": source_id}
        encoded = _json(payload)
        digest = sha256(encoded.encode()).hexdigest()
        deployment = self.deployment(account_id)
        orders = list(payload.get("orders") or [])
        if payload.get("plan_legs"):
            raise ValueError("live executor does not yet accept multi-leg timed plans")
        created = _iso()
        inserted = 0
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT payload_sha256,disposition FROM live_decisions "
                "WHERE account_id=? AND source_decision_id=?",
                (account_id, source_id),
            ).fetchone()
            if existing is not None:
                if existing["payload_sha256"] != digest:
                    raise ValueError("live decision changed for the same source identity")
                return {
                    "decision_id": decision_id, "created": False,
                    "disposition": existing["disposition"], "intents_created": 0,
                }
            self._connection.execute(
                "INSERT INTO live_decisions VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    account_id, decision_id, source_id, str(payload["signal_date"]),
                    str(payload["valid_session"]), qualification, disposition,
                    digest, encoded, created,
                ),
            )
            for sequence, order in enumerate(orders):
                outside_rth = str(order.get("outside_rth", "RTH_ONLY"))
                if outside_rth not in {"RTH_ONLY", "ANY_TIME", "OVERNIGHT"}:
                    raise ValueError("unsupported Longbridge US-stock trading session")
                if outside_rth != "RTH_ONLY" and order["order_type"] != "LIMIT":
                    raise ValueError("extended-hours live orders must be LIMIT")
                spec = {
                    "account_id": account_id, "decision_id": decision_id,
                    "order_sequence": sequence, "arm_generation": deployment["arm_generation"],
                    "symbol": str(payload["symbol"]), "side": str(order["side"]),
                    "quantity": int(order["quantity"]),
                    "order_type": str(order["order_type"]),
                    "outside_rth": outside_rth,
                    "reference_price": str(order["limit_price"]),
                    "time_in_force": str(order["time_in_force"]),
                    "valid_session": str(payload["valid_session"]),
                }
                intent_id = "LBI-" + sha256(
                    f"{account_id}\0{decision_id}\0{sequence}".encode()
                ).hexdigest()[:32].upper()
                client_request_id = "CZSC-" + sha256(intent_id.encode()).hexdigest()[:32].upper()
                intent_payload = {**spec, "intent_id": intent_id, "disposition": disposition}
                self._connection.execute(
                    "INSERT INTO live_intents(intent_id,account_id,decision_id,order_sequence,"
                    "arm_generation,symbol,side,quantity,order_type,outside_rth,reference_price,time_in_force,"
                    "valid_session,client_request_id,status,payload_sha256,payload,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        intent_id, account_id, decision_id, sequence,
                        int(deployment["arm_generation"]), spec["symbol"], spec["side"],
                        spec["quantity"], spec["order_type"], spec["outside_rth"],
                        spec["reference_price"],
                        spec["time_in_force"], spec["valid_session"], client_request_id,
                        disposition, _hash(intent_payload), _json(intent_payload), created, created,
                    ),
                )
                inserted += 1
        self.event("LIVE_DECISION_CAPTURED", account_id=account_id, details={
            "decision_id": decision_id, "source_decision_id": source_id,
            "qualification": qualification, "disposition": disposition,
            "intent_count": inserted,
        })
        return {
            "decision_id": decision_id, "created": True,
            "disposition": disposition, "intents_created": inserted,
        }

    def ready_intents(self, account_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM live_intents WHERE account_id=? AND status='READY' "
                "ORDER BY valid_session,decision_id,order_sequence", (account_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_intent(self, intent_id: str) -> dict[str, Any] | None:
        now = _iso()
        with self._lock, self._connection:
            updated = self._connection.execute(
                "UPDATE live_intents SET status='SUBMITTING',submitted_at=?,updated_at=? "
                "WHERE intent_id=? AND status='READY'",
                (now, now, intent_id),
            ).rowcount
            if not updated:
                return None
            row = self._connection.execute(
                "SELECT * FROM live_intents WHERE intent_id=?", (intent_id,),
            ).fetchone()
        return dict(row)

    def bind_order(self, intent_id: str, order_id: str, *, status: str = "SUBMITTED") -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE live_intents SET status=?,broker_order_id=?,last_error=NULL,updated_at=? "
                "WHERE intent_id=?",
                (status, order_id, _iso(), intent_id),
            )

    def mark_intent(self, intent_id: str, status: str, *, error: str | None = None) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE live_intents SET status=?,last_error=?,updated_at=? WHERE intent_id=?",
                (status, error, _iso(), intent_id),
            )

    def unresolved_intents(self, account_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM live_intents WHERE account_id=? AND status IN "
                "('SUBMITTING','SUBMISSION_UNCERTAIN','SUBMITTED','PARTIAL_FILLED','CANCEL_PENDING',"
                "'CANCEL_UNCERTAIN','REPLACE_PENDING') ORDER BY created_at",
                (account_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def intent(self, intent_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM live_intents WHERE intent_id=?", (intent_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def intent_by_order(self, account_id: str, order_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM live_intents WHERE account_id=? AND broker_order_id=?",
                (account_id, order_id),
            ).fetchone()
        return None if row is None else dict(row)

    def intents(self, account_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM live_intents"
        params: tuple[object, ...] = ()
        if account_id is not None:
            sql += " WHERE account_id=?"
            params = (account_id,)
        sql += " ORDER BY created_at,intent_id"
        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def status(self, account_id: str) -> dict[str, object]:
        deployment = self.deployment(account_id)
        intent_rows = self.intents(account_id)
        return {
            "account_id": account_id,
            "strategy": f"{deployment['strategy_id']}-{deployment['strategy_version']}",
            "release_hash": deployment["release_hash"],
            "symbol": deployment["symbol"],
            "status": deployment["status"],
            "armed": self.armed(account_id),
            "armed_until": deployment["armed_until"],
            "risk_limits": {
                "max_order_notional": deployment["max_order_notional"],
                "max_gross_notional": deployment["max_gross_notional"],
                "cash_reserve": deployment["cash_reserve"],
            },
            "intent_counts": {
                status: sum(row["status"] == status for row in intent_rows)
                for status in sorted({row["status"] for row in intent_rows})
            },
            "last_error": deployment["last_error"],
        }
