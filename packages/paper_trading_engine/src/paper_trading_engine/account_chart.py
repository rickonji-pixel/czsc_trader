"""Account-scoped observation chart builder and PTE-owned cache."""

from __future__ import annotations

from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from threading import Lock, RLock
from typing import Any, Callable
from uuid import uuid4

from .audit import AuditRecorder


ACCOUNT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
INPUT_LIMIT = 5 * 1024 * 1024
OUTPUT_LIMIT = 20 * 1024 * 1024
CACHE_RENDER_REVISION = "external-plotly-runtime-v1"


class AccountChartService:
    def __init__(
        self,
        store,
        *,
        advice,
        cache_dir: Path,
        trader_executable: str | Path = "czsc-trader",
        runner: Callable[..., Any] = subprocess.run,
        timeout_seconds: int = 30,
        context_sessions: int = 60,
        audit: AuditRecorder | None = None,
    ) -> None:
        self.store = store
        self.advice = advice
        self.cache_dir = Path(cache_dir)
        self.trader_executable = str(trader_executable)
        self.runner = runner
        self.timeout_seconds = timeout_seconds
        self.context_sessions = context_sessions
        self.audit = audit or AuditRecorder(store)
        self._locks: dict[str, Lock] = {}
        self._locks_guard = RLock()

    def _account_dir(self, account_id: str) -> Path:
        if ACCOUNT_ID_PATTERN.fullmatch(account_id) is None:
            raise ValueError("invalid account id")
        target = (self.cache_dir / account_id).resolve()
        root = self.cache_dir.resolve()
        if target.parent != root:
            raise ValueError("account chart path escapes cache root")
        return target

    def chart_path(self, account_id: str) -> Path:
        return self._account_dir(account_id) / "observation.html"

    def _meta_path(self, account_id: str) -> Path:
        return self._account_dir(account_id) / "observation.meta.json"

    def _lock(self, account_id: str) -> Lock:
        with self._locks_guard:
            return self._locks.setdefault(account_id, Lock())

    @staticmethod
    def _sha256(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def _market_data(self, account: dict[str, Any]) -> tuple[str, list[dict[str, object]]]:
        price_identity, frame = self.advice.price_history_for_account(
            account_id=str(account["account_id"]),
            strategy_id=str(account["strategy_id"]),
            strategy_version=str(account["strategy_version"]),
            symbol=str(account["symbol"]),
            asset=str(account["asset_type"]),
        )
        frame = frame.rename(
            columns={
                "dt": "date",
                "Date": "date",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
            }
        )
        if not {"date", "open", "high", "low", "close"} <= set(frame.columns):
            raise ValueError("SRT adjusted daily input has incomplete OHLC data")
        bars: dict[str, dict[str, object]] = {}
        for row in frame.to_dict("records"):
            session = date.fromisoformat(str(row["date"])[:10]).isoformat()
            if session in bars:
                raise ValueError(f"duplicate daily market-data session: {session}")
            bars[session] = {
                "date": session,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
        cutoff = date.fromisoformat(str(account["selection_data_cutoff"])).isoformat()
        ordered = [bars[key] for key in sorted(bars)]
        history = [bar for bar in ordered if bar["date"] <= cutoff][-self.context_sessions :]
        forward = [bar for bar in ordered if bar["date"] > cutoff]
        selected = history + forward
        if not selected:
            raise ValueError("no daily market data is available for the account chart")
        return price_identity, selected

    @staticmethod
    def _fact_date(row: dict[str, Any], fields: tuple[str, ...]) -> str | None:
        for field in fields:
            if row.get(field) not in (None, ""):
                return date.fromisoformat(str(row[field])[:10]).isoformat()
        return None

    @staticmethod
    def _decision(row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row.get("payload") or {})
        return {
            "account_id": row["account_id"],
            "decision_id": row["decision_id"],
            "signal_date": row["signal_date"],
            "valid_session": row["valid_session"],
            "generated_at": row["generated_at"],
            "action": payload.get("action"),
            "target_quantity": payload.get("target_quantity"),
            "factor_score": payload.get("factor_score"),
            "regime": payload.get("regime"),
        }

    @staticmethod
    def _intent(row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row.get("payload") or {})
        return {
            "account_id": row.get("account_id"),
            "intent_id": row.get("intent_id"),
            "decision_id": row.get("decision_id"),
            "valid_session": row.get("valid_session") or payload.get("valid_session"),
            "side": row.get("side") or payload.get("side"),
            "quantity": row.get("quantity") or payload.get("quantity"),
            "limit_price": row.get("limit_price") or payload.get("limit_price"),
        }

    def _after_cutoff(
        self, rows: list[dict[str, Any]], cutoff: str, fields: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        return [
            row for row in rows
            if (observed := self._fact_date(row, fields)) is not None and observed > cutoff
        ]

    def _request(self, account_id: str) -> tuple[dict[str, object], bool]:
        account = self.store.virtual_account(account_id)
        cutoff = account.get("selection_data_cutoff")
        if not cutoff:
            raise ValueError("strategy selection cutoff is unavailable")
        cutoff = date.fromisoformat(str(cutoff)).isoformat()
        manifest_sha256, bars = self._market_data(account)
        decisions = self._after_cutoff(
            [self._decision(row) for row in self.store.account_decisions(account_id)],
            cutoff,
            ("signal_date",),
        )
        intents = self._after_cutoff(
            [self._intent(row) for row in self.store.account_intents(account_id)],
            cutoff,
            ("valid_session", "session"),
        )
        fills = []
        for row in self._after_cutoff(
            self.store.account_fills(account_id), cutoff, ("occurred_at", "session"),
        ):
            fills.append({
                "account_id": row.get("account_id"),
                "fill_id": row.get("fill_id"),
                "decision_id": row.get("decision_id"),
                "channel_order_id": row.get("order_id"),
                "occurred_at": row.get("occurred_at"),
                "side": row.get("side"),
                "quantity": row.get("quantity"),
                "price": row.get("price"),
                "fee": row.get("fee"),
            })
        snapshots = [
            {
                "account_id": row.get("account_id"),
                "session": row.get("session"),
                "quantity": row.get("quantity"),
            }
            for row in self._after_cutoff(
                self.store.account_snapshots(account_id), cutoff, ("session",),
            )
        ]
        request = {
            "contract_version": "account_observation.v1",
            "account": {
                "account_id": account_id,
                "strategy_id": account["strategy_id"],
                "strategy_version": account["strategy_version"],
                "release_id": f'{account["strategy_id"]}-{account["strategy_version"]}',
                "release_hash": account["release_hash"],
                "selection_data_cutoff": cutoff,
                "symbol": account["symbol"],
            },
            "context_sessions": self.context_sessions,
            "market_data": {
                "manifest_sha256": manifest_sha256,
                "adjustment": "hfq",
                "bars": bars,
            },
            "decisions": decisions,
            "intents": intents,
            # The renderer has no order layer. Excluding broker payloads keeps this
            # bounded chart contract independent from opaque third-party text.
            "orders": [],
            "fills": fills,
            "snapshots": snapshots,
        }
        return request, any(bar["date"] > cutoff for bar in bars)

    @staticmethod
    def _load_meta(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def current_error(self, account_id: str) -> str | None:
        """Return the persisted chart error without triggering a render."""
        self._account_dir(account_id)
        value = self._load_meta(self._meta_path(account_id)).get("error")
        return str(value) if value else None

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _record_failure(self, account: dict[str, Any], error: Exception) -> None:
        meta_path = self._meta_path(account["account_id"])
        message = str(error)[:500]
        fingerprint = self._sha256(message.encode("utf-8"))
        old = self._load_meta(meta_path)
        if old.get("error_fingerprint") == fingerprint:
            return
        self.audit.record(
            "ACCOUNT_CHART_GENERATION_FAILED",
            source="account_chart",
            outcome="FAILURE",
            actor_type="ENGINE",
            account_id=account["account_id"],
            strategy_id=account.get("strategy_id"),
            strategy_version=account.get("strategy_version"),
            release_hash=account.get("release_hash"),
            symbol=account.get("symbol"),
            details={"operation": "render", "error": message},
        )
        self._atomic_write(
            meta_path,
            json.dumps({"error_fingerprint": fingerprint, "error": message}, ensure_ascii=False),
        )

    def _unavailable(self, account_id: str, error: Exception) -> dict[str, object]:
        try:
            account = self.store.virtual_account(account_id)
            self._record_failure(account, error)
            release_id = f'{account.get("strategy_id")}-{account.get("strategy_version")}'
            cutoff = account.get("selection_data_cutoff")
        except KeyError:
            raise
        return {
            "scope": {"account_id": account_id, "release_id": release_id},
            "status": "UNAVAILABLE",
            "selection_data_cutoff": cutoff,
            "context_sessions": self.context_sessions,
            "chart_url": None,
            "fingerprint": None,
            "message": str(error)[:500],
        }

    def status(self, account_id: str) -> dict[str, object]:
        self._account_dir(account_id)
        try:
            request, has_forward = self._request(account_id)
            encoded = json.dumps(
                request, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
            )
            if len(encoded.encode("utf-8")) > INPUT_LIMIT:
                raise ValueError("account observation input exceeds 5 MiB")
            fingerprint_input = json.dumps(
                request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            # The current chart has no order-status layer. Futu refreshes terminal order
            # timestamps every few seconds, so including orders here would force identical
            # charts to be rendered repeatedly. Intents, fills and snapshots remain visual
            # facts and continue to invalidate the cache.
            cache_key = f"{CACHE_RENDER_REVISION}\n{fingerprint_input}".encode("utf-8")
            fingerprint = self._sha256(cache_key)
        except KeyError:
            raise
        except Exception as exc:
            return self._unavailable(account_id, exc)

        account = request["account"]
        html_path = self.chart_path(account_id)
        meta_path = self._meta_path(account_id)
        with self._lock(account_id):
            old_meta = self._load_meta(meta_path)
            if old_meta.get("fingerprint") != fingerprint or not html_path.is_file():
                try:
                    completed = self.runner(
                        [
                            self.trader_executable,
                            "chart",
                            "observation",
                            "--format",
                            "html",
                            "--plotly-runtime",
                            "external",
                        ],
                        input=encoded,
                        text=True,
                        capture_output=True,
                        shell=False,
                        timeout=self.timeout_seconds,
                        encoding="utf-8",
                    )
                    if completed.returncode:
                        raise RuntimeError(completed.stderr.strip() or "chart renderer failed")
                    html = str(completed.stdout)
                    if len(html.encode("utf-8")) > OUTPUT_LIMIT:
                        raise ValueError("account observation output exceeds 20 MiB")
                    if not html.lstrip().lower().startswith(("<html", "<!doctype html")):
                        raise ValueError("chart renderer returned invalid HTML")
                    self._atomic_write(html_path, html)
                    self._atomic_write(
                        meta_path,
                        json.dumps(
                            {"fingerprint": fingerprint, "has_forward": has_forward},
                            ensure_ascii=False,
                        ),
                    )
                    if old_meta.get("error_fingerprint"):
                        self.audit.record(
                            "ACCOUNT_CHART_RECOVERED",
                            source="account_chart",
                            actor_type="ENGINE",
                            account_id=account_id,
                            strategy_id=account.get("strategy_id"),
                            strategy_version=account.get("strategy_version"),
                            release_hash=account.get("release_hash"),
                            symbol=account.get("symbol"),
                            details={"operation": "render"},
                        )
                except Exception as exc:
                    self._record_failure(account, exc)
                    return self._unavailable(account_id, exc)

        status = "READY" if has_forward else "EMPTY"
        return {
            "scope": {"account_id": account_id, "release_id": account["release_id"]},
            "status": status,
            "selection_data_cutoff": account["selection_data_cutoff"],
            "context_sessions": self.context_sessions,
            "chart_url": f"/charts/{account_id}/observation.html?v={fingerprint}",
            "fingerprint": fingerprint,
            "message": None if has_forward else "等待新的完整收盘数据",
        }
