"""Direct SRT decision adapter for PTE's published local data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import time as clock
from typing import Callable, Mapping

from strategy_runtime import (
    AccountSnapshot,
    ChannelCapabilities,
    DeploymentSpec,
    ExecutionReceipt,
    StrategyLoader,
    StrategyRelease,
    StrategyRunner,
    StrategyStateSnapshot,
    load_strategy_runtime_context,
)

from .errors import AdviceClientError
from .audit import AuditRecorder
from .contracts import AdviceContractError, AdviceDecision


_BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")
_CHANNEL_ID = "futu_simulate_cn"


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdviceClientError(f"cannot read published data manifest {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise AdviceClientError(f"published data manifest {path.name} must be an object")
    return value


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

    def __init__(self, identity: Mapping[str, str]) -> None:
        self.capabilities = ChannelCapabilities(
            ("LIMIT", "MARKET", "MARKETABLE_LIMIT"), ("OPEN", "11:30_CLOSE")
        )
        self.identity = identity
        self.decision: AdviceDecision | None = None

    def submit(self, request, idempotency_key: str) -> ExecutionReceipt:
        references = request.reference_prices
        decision_identity = {
            "release_hash": request.decision.release_hash,
            "signal_date": references.signal_at.date().isoformat(),
            "target_position": request.decision.target_position,
            "runtime_sha256": request.decision.runtime_sha256,
            "inputs": dict(sorted(request.decision.input_identity_hashes.items())),
            "prices": dict(sorted(references.price_identity_hashes.items())),
        }
        suffix = sha256(
            json.dumps(
                decision_identity,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:12].upper()
        source_decision_id = f"SRT-{references.signal_at:%Y%m%d}-{suffix}"
        payload = request.instruction.order_plan_payload()
        payload.update(
            {
                "decision_id": source_decision_id,
                "symbol": request.deployment.symbol,
                "signal_date": references.signal_at.date().isoformat(),
                "valid_session": references.valid_at.date().isoformat(),
                "strategy": dict(self.identity),
                "signal_reference_price": references.signal_reference_price,
                "execution_reference_price": references.execution_reference_price,
                "data_cutoff": references.signal_at.date().isoformat(),
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

    def _load_release(self, strategy_id: str, strategy_version: str) -> StrategyRelease:
        path = self.repo_root / "strategies" / strategy_id / "versions" / f"{strategy_version}.json"
        return StrategyRelease.from_mapping(_load_manifest(path))

    def _strategy_context(
        self,
        strategy_id: str,
        strategy_version: str,
        symbol: str,
        asset: str,
    ):
        release = self._load_release(strategy_id, strategy_version)
        strategy = StrategyLoader().load(release)
        context = load_strategy_runtime_context(self.data_dir, strategy)
        if asset != "etf":
            raise AdviceClientError("PTE currently requires one ETF publication")
        if context.pricing_data.symbol != symbol:
            raise AdviceClientError("SRT execution-pricing symbol differs from account")
        return release, strategy, context

    def runtime_context_for_account(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        symbol: str,
        asset: str,
    ):
        """Return the authenticated SRT runtime context for one PTE account."""

        return self._strategy_context(
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
        total_assets: float,
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
            release, strategy, context = self._strategy_context(
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
                float(Decimal(str(total_assets)).quantize(Decimal("0.01"))),
                int(actual_quantity),
                0,
                generated_at,
            )
            channel = _PteCaptureChannel(identity)
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
                context=context,
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
