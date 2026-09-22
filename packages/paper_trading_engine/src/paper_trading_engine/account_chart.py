"""Account-scoped PTE forward charts refreshed outside request threads."""

from __future__ import annotations

from concurrent.futures import Executor, Future, ThreadPoolExecutor
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from threading import RLock
import time
from typing import Any, Callable
from uuid import uuid4

from .audit import AuditRecorder
from .forward_chart import FORWARD_CHART_CONTRACT_VERSION, render_forward_chart_html


ACCOUNT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
INPUT_LIMIT = 5 * 1024 * 1024
OUTPUT_LIMIT = 20 * 1024 * 1024
CACHE_RENDER_REVISION = "pte-forward-chart-v1"


class AccountChartService:
    """Serve cached charts and run DFLS plus rendering on one dedicated worker."""

    def __init__(
        self,
        store,
        *,
        market_data,
        cache_dir: Path,
        context_sessions: int = 60,
        refresh_interval_seconds: float = 60,
        audit: AuditRecorder | None = None,
        executor: Executor | None = None,
        renderer: Callable[[object], str] = render_forward_chart_html,
    ) -> None:
        self.store = store
        self.market_data = market_data
        self.cache_dir = Path(cache_dir)
        self.context_sessions = context_sessions
        self.refresh_interval_seconds = refresh_interval_seconds
        self.audit = audit or AuditRecorder(store)
        self.renderer = renderer
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="pte-account-chart",
        )
        self._owns_executor = executor is None
        self._jobs: dict[str, Future[None]] = {}
        self._last_submit: dict[str, float] = {}
        self._guard = RLock()

    def _account_dir(self, account_id: str) -> Path:
        if ACCOUNT_ID_PATTERN.fullmatch(account_id) is None:
            raise ValueError("invalid account id")
        root = self.cache_dir.resolve()
        target = (root / account_id).resolve()
        if target.parent != root:
            raise ValueError("account chart path escapes cache root")
        return target

    def chart_path(self, account_id: str) -> Path:
        return self._account_dir(account_id) / "observation.html"

    def _meta_path(self, account_id: str) -> Path:
        return self._account_dir(account_id) / "observation.meta.json"

    @staticmethod
    def _sha256(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def _market_data(self, account: dict[str, Any]) -> tuple[str, list[dict[str, object]]]:
        cutoff = date.fromisoformat(str(account["selection_data_cutoff"])).isoformat()
        price_identity, frame = self.market_data.history(
            symbol=str(account["symbol"]),
            asset=str(account["asset_type"]),
            selection_data_cutoff=cutoff,
            context_sessions=self.context_sessions,
        )
        frame = frame.rename(
            columns={
                "dt": "date", "Date": "date", "Open": "open", "High": "high",
                "Low": "low", "Close": "close",
            }
        )
        if not {"date", "open", "high", "low", "close"} <= set(frame.columns):
            raise ValueError("DFLS adjusted daily input has incomplete OHLC data")
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
        ordered = [bars[key] for key in sorted(bars)]
        history = [bar for bar in ordered if bar["date"] <= cutoff][-self.context_sessions :]
        selected = history + [bar for bar in ordered if bar["date"] > cutoff]
        if not selected:
            raise ValueError("no daily market data is available for the account chart")
        return str(price_identity), selected

    @staticmethod
    def _fact_date(row: dict[str, Any], fields: tuple[str, ...]) -> str | None:
        for field in fields:
            if row.get(field) not in (None, ""):
                return date.fromisoformat(str(row[field])[:10]).isoformat()
        return None

    @staticmethod
    def _decision(row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row.get("payload") or {})
        observation = payload.get("observation")
        if not isinstance(observation, dict) or observation.get("status") != "READY":
            message = observation.get("message") if isinstance(observation, dict) else None
            raise ValueError(message or f'decision {row["decision_id"]} has no chart observation')
        return {
            "account_id": row["account_id"],
            "decision_id": row["decision_id"],
            "signal_date": row["signal_date"],
            "valid_session": row["valid_session"],
            "generated_at": row["generated_at"],
            "action": payload.get("action"),
            "target_quantity": payload.get("target_quantity"),
            "observation": observation,
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
        cutoff_value = account.get("selection_data_cutoff")
        if not cutoff_value:
            raise ValueError("strategy selection cutoff is unavailable")
        cutoff = date.fromisoformat(str(cutoff_value)).isoformat()
        market_identity, bars = self._market_data(account)
        decisions = [
            self._decision(row)
            for row in self._after_cutoff(
                self.store.account_decisions(account_id), cutoff, ("signal_date",)
            )
        ]
        intents = [
            self._intent(row)
            for row in self._after_cutoff(
                self.store.account_intents(account_id),
                cutoff,
                ("valid_session", "session"),
            )
        ]
        fills = [
            {
                "account_id": row.get("account_id"),
                "fill_id": row.get("fill_id"),
                "decision_id": row.get("decision_id"),
                "channel_order_id": row.get("order_id"),
                "occurred_at": row.get("occurred_at"),
                "side": row.get("side"),
                "quantity": row.get("quantity"),
                "price": row.get("price"),
                "fee": row.get("fee"),
            }
            for row in self._after_cutoff(
                self.store.account_fills(account_id), cutoff, ("occurred_at", "session"),
            )
        ]
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
        release_id = f'{account["strategy_id"]}-{account["strategy_version"]}'
        request = {
            "contract_version": FORWARD_CHART_CONTRACT_VERSION,
            "strategy": {
                "account_id": account_id,
                "strategy_id": account["strategy_id"],
                "version": account["strategy_version"],
                "release_id": release_id,
                "release_hash": account["release_hash"],
                "name": account["strategy_name_snapshot"],
                "symbol": account["symbol"],
            },
            "window": {
                "selection_data_cutoff": cutoff,
                "context_sessions": self.context_sessions,
            },
            "market_data": {
                "identity": market_identity,
                "adjustment": "hfq",
                "as_of": bars[-1]["date"],
                "bars": bars,
            },
            "observations": decisions,
            "execution": {"intents": intents, "fills": fills, "snapshots": snapshots},
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
        """Return the persisted chart error without triggering a refresh."""
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
        meta_path = self._meta_path(str(account["account_id"]))
        message = str(error)[:500]
        error_fingerprint = self._sha256(message.encode("utf-8"))
        old = self._load_meta(meta_path)
        if old.get("error_fingerprint") != error_fingerprint:
            self.audit.record(
                "ACCOUNT_CHART_GENERATION_FAILED", source="account_chart",
                outcome="FAILURE", actor_type="ENGINE", account_id=account["account_id"],
                strategy_id=account.get("strategy_id"),
                strategy_version=account.get("strategy_version"),
                release_hash=account.get("release_hash"), symbol=account.get("symbol"),
                details={"operation": "render", "error": message},
            )
        old.update({"error_fingerprint": error_fingerprint, "error": message})
        self._atomic_write(meta_path, json.dumps(old, ensure_ascii=False))

    def _refresh(self, account_id: str) -> None:
        account = self.store.virtual_account(account_id)
        meta_path = self._meta_path(account_id)
        old_meta = self._load_meta(meta_path)
        try:
            request, has_forward = self._request(account_id)
            encoded = json.dumps(
                request, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                allow_nan=False, default=str,
            )
            if len(encoded.encode("utf-8")) > INPUT_LIMIT:
                raise ValueError("account observation input exceeds 5 MiB")
            fingerprint = self._sha256(f"{CACHE_RENDER_REVISION}\n{encoded}".encode("utf-8"))
            html_path = self.chart_path(account_id)
            if old_meta.get("fingerprint") != fingerprint or not html_path.is_file():
                html = self.renderer(request)
                if len(html.encode("utf-8")) > OUTPUT_LIMIT:
                    raise ValueError("account observation output exceeds 20 MiB")
                if not html.lstrip().lower().startswith(("<html", "<!doctype html")):
                    raise ValueError("chart renderer returned invalid HTML")
                self._atomic_write(html_path, html)
            self._atomic_write(
                meta_path,
                json.dumps(
                    {
                        "fingerprint": fingerprint, "has_forward": has_forward,
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                    },
                    ensure_ascii=False,
                ),
            )
            if old_meta.get("error_fingerprint"):
                self.audit.record(
                    "ACCOUNT_CHART_RECOVERED", source="account_chart", actor_type="ENGINE",
                    account_id=account_id, strategy_id=account.get("strategy_id"),
                    strategy_version=account.get("strategy_version"),
                    release_hash=account.get("release_hash"), symbol=account.get("symbol"),
                    details={"operation": "render"},
                )
        except Exception as exc:
            self._record_failure(account, exc)

    @staticmethod
    def _scope(account: dict[str, Any]) -> dict[str, str]:
        return {
            "account_id": str(account["account_id"]),
            "release_id": f'{account["strategy_id"]}-{account["strategy_version"]}',
        }

    def _status_from_cache(
        self, account: dict[str, Any], *, refreshing: bool,
    ) -> dict[str, object]:
        account_id = str(account["account_id"])
        meta = self._load_meta(self._meta_path(account_id))
        fingerprint = str(meta.get("fingerprint") or "")
        chart_exists = self.chart_path(account_id).is_file()
        base = {
            "scope": self._scope(account),
            "selection_data_cutoff": account.get("selection_data_cutoff"),
            "context_sessions": self.context_sessions,
        }
        if meta.get("error"):
            return {
                **base, "status": "UNAVAILABLE", "chart_url": None,
                "fingerprint": None, "message": str(meta["error"]),
            }
        if fingerprint and chart_exists:
            has_forward = bool(meta.get("has_forward"))
            return {
                **base,
                "status": "REFRESHING" if refreshing else ("READY" if has_forward else "EMPTY"),
                "chart_url": f"/charts/{account_id}/observation.html?v={fingerprint}",
                "fingerprint": fingerprint,
                "message": "正在刷新观察图" if refreshing else (
                    None if has_forward else "等待新的完整收盘数据"
                ),
            }
        return {
            **base, "status": "BUILDING", "chart_url": None,
            "fingerprint": None, "message": "正在生成观察图",
        }

    def status(self, account_id: str) -> dict[str, object]:
        """Return immediately; enqueue at most one refresh for this account."""
        self._account_dir(account_id)
        account = self.store.virtual_account(account_id)
        now = time.monotonic()
        with self._guard:
            job = self._jobs.get(account_id)
            if job is not None and job.done():
                self._jobs.pop(account_id, None)
                job = None
            last_submit = self._last_submit.get(account_id)
            if job is None and (
                last_submit is None or now - last_submit >= self.refresh_interval_seconds
            ):
                job = self._executor.submit(self._refresh, account_id)
                self._jobs[account_id] = job
                self._last_submit[account_id] = now
                if job.done():
                    self._jobs.pop(account_id, None)
                    job = None
            return self._status_from_cache(account, refreshing=job is not None)

    def close(self) -> None:
        if self._owns_executor:
            self._executor.shutdown(wait=True, cancel_futures=True)
