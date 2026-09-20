"""Direct SRT decision adapter for PTE's published local data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import time as clock
from typing import Callable, Mapping

import pandas as pd
from strategy_runtime import (
    AccountSnapshot,
    ChannelCapabilities,
    DeploymentSpec,
    ExecutionReceipt,
    StrategyLoader,
    StrategyRelease,
    StrategyRunner,
    StrategyStateSnapshot,
    build_execution_plan,
    load_strategy_publication,
)

from .errors import AdviceClientError
from .audit import AuditRecorder
from .contracts import AdviceContractError, AdviceDecision


_BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")
_CHANNEL_ID = "futu_simulate_cn"


def _published(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rename(
        columns={
            "date": "Date",
            "dt": "Date",
            "datetime": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
            "vol": "Volume",
            "amount": "Amount",
        }
    ).drop(columns=["symbol"], errors="ignore")


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdviceClientError(f"cannot read published data manifest {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise AdviceClientError(f"published data manifest {path.name} must be an object")
    return value


@dataclass(frozen=True)
class _PublishedInputs:
    cutoff: pd.Timestamp
    next_session: pd.Timestamp
    signal_close: float
    execution_close: float


def _publication_inputs(publication) -> _PublishedInputs:
    try:
        daily = _published(publication.input_results["adjusted_daily"].dataframe)
        execution_daily = _published(
            publication.input_results["execution_daily"].dataframe
        )
        calendar = _published(
            publication.input_results["trading_calendar"].dataframe
        )
        cutoff = pd.Timestamp(publication.requested_cutoff).normalize()
    except (KeyError, TypeError, ValueError) as exc:
        raise AdviceClientError(
            "SRT publication has no complete execution context"
        ) from exc
    if not {"Date", "Close"} <= set(daily.columns) or not {
        "Date",
        "Close",
    } <= set(execution_daily.columns):
        raise AdviceClientError("SRT publication daily inputs are incomplete")
    if not {"Date", "IsOpen"} <= set(calendar.columns):
        raise AdviceClientError("SRT publication trading calendar is incomplete")
    daily_dates = pd.to_datetime(daily["Date"]).dt.normalize()
    execution_dates = pd.to_datetime(execution_daily["Date"]).dt.normalize()
    daily_row = daily.loc[daily_dates.eq(cutoff)]
    execution_row = execution_daily.loc[execution_dates.eq(cutoff)]
    if len(daily_row) != 1 or len(execution_row) != 1:
        raise AdviceClientError(
            "SRT publication does not contain exactly one cutoff price row"
        )
    calendar_dates = pd.to_datetime(calendar["Date"], errors="coerce").dt.normalize()
    open_mask = pd.to_numeric(calendar["IsOpen"], errors="coerce").eq(1)
    following = calendar_dates.loc[open_mask & calendar_dates.gt(cutoff)]
    if following.empty:
        raise AdviceClientError(
            "SRT publication has no next open trading session"
        )
    return _PublishedInputs(
        cutoff,
        pd.Timestamp(following.min()),
        float(daily_row.iloc[0]["Close"]),
        float(execution_row.iloc[0]["Close"]),
    )


def _strategy_identity(repo_root: Path, release: StrategyRelease) -> dict[str, str]:
    root = repo_root / "strategies" / release.strategy_family_id
    family = _load_manifest(root / "family.json")
    lifecycle_path = root / "lifecycle.jsonl"
    qualification = None
    for line in lifecycle_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("version") == release.version and event.get("release_hash") == release.release_hash:
            qualification = event.get("to_state")
    if qualification not in {"PAPER_READY", "LIVE_READY"}:
        raise AdviceClientError(f"{release.release_id} is not approved for paper trading")
    return {
        "strategy_id": release.strategy_family_id,
        "name": str(family["name"]),
        "version": release.version,
        "release_id": release.release_id,
        "release_hash": release.release_hash,
        "qualification": str(qualification),
    }


class _PteCaptureChannel:
    channel_id = _CHANNEL_ID

    def __init__(self, identity: Mapping[str, str], published: _PublishedInputs) -> None:
        self.capabilities = ChannelCapabilities(
            ("LIMIT", "MARKET", "MARKETABLE_LIMIT"), ("OPEN", "11:30_CLOSE")
        )
        self.identity = identity
        self.published = published
        self.decision: AdviceDecision | None = None

    def submit(self, request, idempotency_key: str) -> ExecutionReceipt:
        decision_identity = {
            "release_hash": request.decision.release_hash,
            "signal_date": self.published.cutoff.date().isoformat(),
            "target_position": request.decision.target_position,
            "runtime_sha256": request.decision.runtime_sha256,
            "inputs": dict(sorted(request.decision.input_identity_hashes.items())),
        }
        suffix = sha256(
            json.dumps(
                decision_identity,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:12].upper()
        source_decision_id = f"SRT-{self.published.cutoff:%Y%m%d}-{suffix}"
        payload = dict(
            build_execution_plan(
                request,
                signal_reference_price=self.published.signal_close,
                execution_reference_price=self.published.execution_close,
            )
        )
        payload.update(
            {
                "decision_id": source_decision_id,
                "symbol": request.deployment.symbol,
                "signal_date": self.published.cutoff.date().isoformat(),
                "valid_session": self.published.next_session.date().isoformat(),
                "strategy": dict(self.identity),
                "signal_reference_price": self.published.signal_close,
                "execution_reference_price": self.published.execution_close,
                "data_cutoff": self.published.cutoff.date().isoformat(),
                "runtime_sha256": request.decision.runtime_sha256,
                "input_identity_hashes": dict(request.decision.input_identity_hashes),
            }
        )
        try:
            self.decision = AdviceDecision.from_cli_payload(
                {"status": "PASS", "result": payload}
            )
        except AdviceContractError as exc:
            raise AdviceClientError(f"SRT execution request violates PTE contract: {exc}") from exc
        return ExecutionReceipt(idempotency_key, True, self.decision.decision_id, "CAPTURED", "captured by PTE adapter")


class SrtAdviceClient:
    """Generate PTE decisions directly from frozen SRT classes and local publications."""

    def __init__(
        self,
        *,
        repo_root: Path,
        data_dir: Path,
        symbol: str | None = None,
        asset: str = "etf",
        audit: AuditRecorder | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.symbol = symbol.upper() if symbol else None
        self.asset = asset
        self.audit = audit
        self.now = now or (lambda: datetime.now(_BEIJING))
        self._published_cache: dict[tuple[str, str, str], _PublishedInputs] = {}

    def _load_release(self, strategy_id: str, strategy_version: str) -> StrategyRelease:
        path = self.repo_root / "strategies" / strategy_id / "versions" / f"{strategy_version}.json"
        return StrategyRelease.from_mapping(_load_manifest(path))

    @staticmethod
    def _publication_key(symbol: str, publication) -> tuple[str, str, str]:
        identity = {
            "release_id": publication.release_id,
            "release_hash": publication.release_hash,
            "requested_cutoff": publication.requested_cutoff,
            "inputs": {
                name: result.identity.content_sha256
                for name, result in sorted(publication.input_results.items())
            },
        }
        digest = sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return symbol, publication.release_id, digest

    def _strategy_publication(
        self,
        strategy_id: str,
        strategy_version: str,
        symbol: str,
        asset: str,
    ):
        release = self._load_release(strategy_id, strategy_version)
        strategy = StrategyLoader().load(release)
        publication = load_strategy_publication(self.data_dir, strategy)
        if asset != "etf":
            raise AdviceClientError("PTE currently requires one ETF publication")
        subjects = {
            str(request.symbol).upper()
            for request in publication.input_requests.values()
            if str(request.dataset).startswith("etf.") and request.symbol
        }
        if subjects != {symbol}:
            raise AdviceClientError("SRT publication symbol differs from account")
        return release, strategy, publication

    def publication_for_account(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        symbol: str,
        asset: str,
    ):
        """Return the authenticated SRT publication bound to one PTE account."""

        return self._strategy_publication(
            strategy_id,
            strategy_version,
            symbol.upper(),
            asset,
        )[2]

    def _audit_call(self, started: float, *, error=None, **scope) -> None:
        if self.audit is None or error is None:
            return
        correlation = f"srt:{scope.get('symbol')}"
        self.audit.record(
            "DECISION_GENERATION_FAILED",
            source="srt_advice_client",
            outcome="FAILURE",
            actor_type="ENGINE",
            actor_id="strategy_runtime",
            correlation_id=correlation,
            details={
                "service": "strategy_runtime",
                "operation": "calculate_history_and_submit",
                "duration_ms": round((clock.perf_counter() - started) * 1000, 3),
                "error_type": type(error).__name__,
                "error": str(error),
            },
            **scope,
        )

    def get_decision(
        self,
        actual_quantity: int,
        available_cash: float,
        cycle_target_quantity: int | None = None,
        strategy_id: str | None = None,
        strategy_version: str | None = None,
        baseline: str | None = None,
        account_id: str | None = None,
        symbol: str | None = None,
        asset: str | None = None,
        decision_transform: Callable[[AdviceDecision], AdviceDecision] | None = None,
    ) -> AdviceDecision:
        del baseline
        started = clock.perf_counter()
        selected_symbol = (symbol or self.symbol or "").upper()
        selected_asset = asset or self.asset
        scope = {
            "account_id": account_id,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "symbol": selected_symbol,
        }
        try:
            if not strategy_id or not strategy_version or not account_id:
                raise AdviceClientError("SRT advice requires strategy, version, and account")
            if selected_asset != "etf" or not selected_symbol:
                raise AdviceClientError("SRT advice requires one ETF symbol")
            release, strategy, publication = self._strategy_publication(
                strategy_id,
                strategy_version,
                selected_symbol,
                selected_asset,
            )
            if strategy.definition.state_mode != "STATELESS":
                raise AdviceClientError(
                    "PTE does not support persisted SRT strategy state yet"
                )
            identity = _strategy_identity(self.repo_root, release)
            publication_key = self._publication_key(selected_symbol, publication)
            published = self._published_cache.get(publication_key)
            if published is None:
                published = _publication_inputs(publication)
                self._published_cache = {
                    key: value
                    for key, value in self._published_cache.items()
                    if key[:2] != publication_key[:2]
                }
                self._published_cache[publication_key] = published
            generated_at = self.now()
            if generated_at.tzinfo is None:
                generated_at = generated_at.replace(tzinfo=_BEIJING)
            else:
                generated_at = generated_at.astimezone(_BEIJING)
            deployment = DeploymentSpec(
                f"pte:{account_id}",
                release.release_id,
                release.release_hash,
                selected_symbol,
                account_id,
                _CHANNEL_ID,
                {"cycle_target_quantity": cycle_target_quantity},
            )
            account = AccountSnapshot(
                account_id,
                float(Decimal(str(available_cash)).quantize(Decimal("0.01"))),
                float(Decimal(str(available_cash)).quantize(Decimal("0.01")))
                + actual_quantity * published.execution_close,
                int(actual_quantity),
                0,
                generated_at,
            )
            channel = _PteCaptureChannel(identity, published)
            state = StrategyStateSnapshot(
                deployment.deployment_id,
                release.release_hash,
                0,
                generated_at,
                {},
            )

            class SnapshotAccount:
                def snapshot(self, _deployment):
                    return account

            result = StrategyRunner().run_published(
                strategy=strategy,
                deployment=deployment,
                state=state,
                publication=publication,
                account=SnapshotAccount(),
                channel=channel,
                calculation_time=generated_at,
            )
            if result.decision is None:
                raise AdviceClientError("SRT returned no strategy decision")
            if channel.decision is None:
                raise AdviceClientError("SRT execution channel returned no PTE decision")
            decision = channel.decision
            if decision_transform is not None:
                decision = decision_transform(decision)
        except Exception as exc:
            self._audit_call(started, error=exc, **scope)
            if isinstance(exc, AdviceClientError):
                raise
            raise AdviceClientError(f"SRT decision generation failed: {exc}") from exc
        return decision
