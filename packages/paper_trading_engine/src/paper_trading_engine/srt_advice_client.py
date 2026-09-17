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
    read_publication,
)

from .advice_client import AdviceClientError
from .audit import AuditRecorder
from .contracts import AdviceContractError, AdviceDecision


_BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")
_CHANNEL_ID = "futu_simulate_cn"


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


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


def _verify_generation(data_dir: Path, symbol: str) -> dict[str, object]:
    code = symbol.split(".", 1)[0]
    manifest = _load_manifest(data_dir / f"{code}_strategy_generation.json")
    if manifest.get("schema_version") != 2:
        raise AdviceClientError("published strategy generation schema is unsupported")
    if str(manifest.get("symbol", "")).upper() != symbol.upper():
        raise AdviceClientError("published strategy generation symbol differs from account")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise AdviceClientError("published strategy generation has no authenticated files")
    for name, expected in files.items():
        if not isinstance(name, str) or not isinstance(expected, str):
            raise AdviceClientError("published strategy generation file entry is invalid")
        path = data_dir / name
        if not path.is_file() or _sha256(path) != expected:
            raise AdviceClientError(
                f"published strategy generation is incomplete or mixed: {name}"
            )
    return manifest


def _load_manifest_frames(data_dir: Path, manifest: Mapping[str, object]) -> dict[str, pd.DataFrame]:
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise AdviceClientError("published data manifest has no files")
    grouped: dict[str, list[pd.DataFrame]] = {}
    for name, metadata in files.items():
        if not isinstance(name, str) or not isinstance(metadata, dict):
            raise AdviceClientError("published data manifest file entry is invalid")
        path = data_dir / name
        expected = metadata.get("sha256")
        if not isinstance(expected, str) or _sha256(path) != expected:
            raise AdviceClientError(f"published data file hash differs from manifest: {name}")
        frequency = str(metadata.get("frequency", ""))
        grouped.setdefault(frequency, []).append(pd.read_csv(path))
    result: dict[str, pd.DataFrame] = {}
    for frequency, frames in grouped.items():
        frame = pd.concat(frames, ignore_index=True)
        date_column = next(
            (candidate for candidate in ("date", "dt", "datetime") if candidate in frame),
            None,
        )
        if date_column is None:
            raise AdviceClientError(f"published {frequency} data has no date column")
        result[frequency] = frame.sort_values(date_column)
    return result


@dataclass(frozen=True)
class _PublishedInputs:
    cutoff: pd.Timestamp
    next_session: pd.Timestamp
    inputs: Mapping[str, pd.DataFrame]
    identity: str
    signal_close: float
    execution_close: float


def _load_inputs(data_dir: Path, symbol: str) -> _PublishedInputs:
    _verify_generation(data_dir, symbol)
    code = symbol.split(".", 1)[0]
    adjusted_manifest_path = data_dir / f"{code}_manifest.json"
    execution_manifest_path = data_dir / f"{code}_execution_manifest.json"
    validation_path = data_dir / f"{code}_validation.json"
    adjusted_manifest = _load_manifest(adjusted_manifest_path)
    execution_manifest = _load_manifest(execution_manifest_path)
    validation = _load_manifest(validation_path)
    if validation.get("status") != "PASS":
        raise AdviceClientError(f"published data validation is not PASS for {symbol}")
    if str(adjusted_manifest.get("symbol", "")).upper() != symbol:
        raise AdviceClientError("adjusted-data manifest symbol differs from account")
    if str(execution_manifest.get("symbol", "")).upper() != symbol:
        raise AdviceClientError("execution-data manifest symbol differs from account")
    adjusted_cutoff = pd.Timestamp(str(adjusted_manifest.get("requested_end"))).normalize()
    execution_cutoff = pd.Timestamp(str(execution_manifest.get("requested_end"))).normalize()
    if adjusted_cutoff != execution_cutoff:
        raise AdviceClientError("adjusted and execution publication cutoffs differ")
    try:
        next_session = pd.Timestamp(str(execution_manifest["next_trading_session"])).normalize()
    except (KeyError, ValueError) as exc:
        raise AdviceClientError("execution manifest has no valid next trading session") from exc
    if next_session <= adjusted_cutoff:
        raise AdviceClientError("next trading session must follow the published cutoff")

    adjusted = _load_manifest_frames(data_dir, adjusted_manifest)
    execution = _load_manifest_frames(data_dir, execution_manifest)
    if "daily" not in adjusted or "daily" not in execution:
        raise AdviceClientError("published daily or execution data is missing")
    daily = _published(adjusted["daily"])
    execution_daily = _published(execution["daily"])
    daily_dates = pd.to_datetime(daily["Date"]).dt.normalize()
    execution_dates = pd.to_datetime(execution_daily["Date"]).dt.normalize()
    daily_row = daily.loc[daily_dates.eq(adjusted_cutoff)]
    execution_row = execution_daily.loc[execution_dates.eq(adjusted_cutoff)]
    if len(daily_row) != 1 or len(execution_row) != 1:
        raise AdviceClientError("published data does not contain exactly one cutoff row")

    inputs: dict[str, pd.DataFrame] = {
        "adjusted_daily": daily,
        "execution_daily": execution_daily,
    }
    if "30m" in adjusted:
        inputs["adjusted_30m"] = _published(adjusted["30m"])
    if "weekly" in adjusted:
        inputs["adjusted_weekly"] = _published(adjusted["weekly"])
    digest = sha256()
    identity_paths = [adjusted_manifest_path, execution_manifest_path, validation_path]
    for path in identity_paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return _PublishedInputs(
        adjusted_cutoff,
        next_session,
        inputs,
        digest.hexdigest(),
        float(daily_row.iloc[0]["Close"]),
        float(execution_row.iloc[0]["Close"]),
    )


def _strategy_identity(repo_root: Path, release: StrategyRelease) -> dict[str, str]:
    root = repo_root / "strategies" / release.strategy_family_id
    family = _load_manifest(root / "strategy.json")
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
        self._srt_publication_cache: dict[tuple[str, str], object] = {}

    def _load_release(self, strategy_id: str, strategy_version: str) -> StrategyRelease:
        path = self.repo_root / "strategies" / strategy_id / "versions" / f"{strategy_version}.json"
        return StrategyRelease.from_mapping(_load_manifest(path))

    def _publication_key(self, symbol: str, release_id: str) -> tuple[str, str, str]:
        digest = sha256(self.data_identity(symbol).encode("ascii"))
        return symbol, release_id, digest.hexdigest()

    def _stored_publication(self, release_id: str):
        path = self.data_dir / f"srt_{release_id.lower().replace('-', '_')}_publication.json"
        identity = _sha256(path)
        key = (release_id, identity)
        publication = self._srt_publication_cache.get(key)
        if publication is None:
            publication = read_publication(self.data_dir, release_id)
            self._srt_publication_cache = {
                cached_key: value
                for cached_key, value in self._srt_publication_cache.items()
                if cached_key[0] != release_id
            }
            self._srt_publication_cache[key] = publication
        return publication

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
            release = self._load_release(strategy_id, strategy_version)
            strategy = StrategyLoader().load(release)
            if strategy.definition.state_mode != "STATELESS":
                raise AdviceClientError(
                    "PTE does not support persisted SRT strategy state yet"
                )
            identity = _strategy_identity(self.repo_root, release)
            publication_key = self._publication_key(selected_symbol, release.release_id)
            published = self._published_cache.get(publication_key)
            if published is None:
                published = _load_inputs(self.data_dir, selected_symbol)
                self._published_cache = {
                    key: value
                    for key, value in self._published_cache.items()
                    if key[:2] != publication_key[:2]
                }
                self._published_cache[publication_key] = published
            publication = self._stored_publication(release.release_id)
            cutoff = published.cutoff
            if pd.Timestamp(publication.requested_cutoff).normalize() != cutoff:
                raise AdviceClientError("SRT and operational publication cutoffs differ")
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

    def data_identity(self, symbol: str | None = None) -> str:
        selected_symbol = (symbol or self.symbol or "").upper()
        if not selected_symbol:
            raise AdviceClientError("data identity symbol is required")
        code = selected_symbol.split(".", 1)[0]
        generation = _verify_generation(self.data_dir, selected_symbol)
        digest = sha256()
        digest.update(str(generation["generation_id"]).encode("utf-8"))
        for name in (
            f"{code}_manifest.json",
            f"{code}_validation.json",
            f"{code}_execution_manifest.json",
        ):
            path = self.data_dir / name
            digest.update(name.encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()
