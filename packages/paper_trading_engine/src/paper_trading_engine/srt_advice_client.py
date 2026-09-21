"""PTE adapter from SRT execution plans to durable account decisions."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import time as clock
from typing import Callable

from strategy_runtime import (
    DataPreparationResult,
    ExecutionPlan,
    ExecutionState,
    PortfolioSnapshot,
    StrategyInit,
    StrategyInstance,
    StrategyRelease,
    StrategyRuntime,
    TradableWindow,
    TradingPoint,
    canonical_sha256,
)
from dataflows import canonical_frame_sha256

from .audit import AuditRecorder
from .contracts import AdviceContractError, AdviceDecision
from .errors import AdviceClientError


_BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdviceClientError(f"cannot read prepared-data index {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise AdviceClientError(f"prepared-data index {path.name} must be an object")
    return value


def _strategy_identity(repo_root: Path, release: StrategyRelease) -> dict[str, str]:
    root = repo_root / "strategies" / release.strategy_family_id
    family = _load_manifest(root / "family.json")
    qualification = None
    for line in (root / "lifecycle.jsonl").read_text(encoding="utf-8").splitlines():
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


def _order_payload(order) -> dict[str, object]:
    if order.limit_price is None:
        raise AdviceClientError("PTE requires an executable reference price for every order")
    return {
        "side": order.side.value,
        "quantity": order.quantity,
        "order_type": order.order_type.value,
        "limit_price": float(order.limit_price),
        "time_in_force": "DAY",
    }


def _decision_from_plan(plan: ExecutionPlan, identity: dict[str, str]) -> AdviceDecision:
    orders = [_order_payload(order) for order in plan.orders]
    legs = [
        {
            "sequence": leg.sequence,
            "role": leg.role,
            "checkpoint": leg.checkpoint,
            "submit_after": leg.submit_after.isoformat(),
            "submit_before": leg.submit_before.isoformat(),
            "dependency_sequence": leg.dependency_sequence,
            "dependency_required_status": leg.dependency_required_status,
            "order": _order_payload(leg.order),
        }
        for leg in plan.legs
    ]
    source_decision_id = f"SRT-{plan.signal_date:%Y%m%d}-{plan.signal_identity[:12].upper()}"
    payload = {
        "contract_version": "advice.v5" if plan.plan_mode != "NONE" or legs else "advice.v4",
        "decision_id": source_decision_id,
        "source_decision_id": source_decision_id,
        "signal_identity": plan.signal_identity,
        "plan_identity": plan.plan_identity,
        "portfolio_revision": plan.expected_portfolio_revision,
        "state_revision": plan.expected_state_revision,
        "symbol": plan.symbol,
        "signal_date": plan.signal_date.isoformat(),
        "valid_session": plan.trading_date.isoformat(),
        "actual_quantity": plan.actual_quantity,
        "target_quantity": plan.target_quantity,
        "cycle_target_quantity": plan.cycle_target_quantity,
        "delta_quantity": plan.target_quantity - plan.actual_quantity,
        "action": plan.action,
        "strategy": identity,
        "signal_reference_price": float(plan.references.signal_price),
        "execution_reference_price": float(plan.references.execution_price),
        "data_cutoff": plan.signal_date.isoformat(),
        "order": orders[0] if len(orders) == 1 else None,
        "orders": orders,
        "available_cash": float(plan.available_cash),
        "fee_rate": float(plan.fee_rate),
        "estimated_order_cost": float(plan.estimated_order_cost),
        "unallocated_cash": float(plan.unallocated_cash),
        "capital_rule": {
            "mode": plan.capital_mode,
            "allocation_fraction": float(plan.allocation_fraction),
            "target_scope": "entry_cycle",
        },
        "plan_mode": plan.plan_mode,
        "plan_legs": legs,
        "runtime_sha256": plan.strategy.runtime_sha256,
        "input_identity_hashes": dict(plan.input_identities),
    }
    try:
        return AdviceDecision.from_cli_payload({"status": "PASS", "result": payload})
    except AdviceContractError as exc:
        raise AdviceClientError(f"SRT execution plan violates PTE contract: {exc}") from exc


class SrtAdviceClient:
    """Generate PTE decisions from the caller-neutral SRT API."""

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

    def _entry(self, release_id: str) -> dict[str, object]:
        index = _load_manifest(self.data_dir / "prepared-data-index.json")
        index_hash = index.pop("index_sha256", None)
        if index_hash != canonical_sha256(index):
            raise AdviceClientError("prepared-data index was modified")
        if index.get("schema_version") != 1:
            raise AdviceClientError("prepared-data index version is unsupported")
        releases = index.get("releases")
        if not isinstance(releases, dict) or not isinstance(releases.get(release_id), dict):
            raise AdviceClientError(f"prepared data is unavailable for {release_id}")
        entry = dict(releases[release_id])
        entry["symbol"] = index.get("symbol")
        entry["signal_date"] = index.get("signal_date")
        entry["trading_date"] = index.get("trading_date")
        return entry

    def prepared_through(self, strategy_id: str, strategy_version: str) -> date:
        entry = self._entry(f"{strategy_id}-{strategy_version}")
        try:
            return date.fromisoformat(str(entry["signal_date"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise AdviceClientError("prepared-data signal date is invalid") from exc

    def tradable_date(self, strategy_id: str, strategy_version: str) -> date:
        entry = self._entry(f"{strategy_id}-{strategy_version}")
        try:
            return date.fromisoformat(str(entry["trading_date"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise AdviceClientError("prepared-data trading date is invalid") from exc

    def _instance(
        self, release: StrategyRelease, trading_date: date
    ) -> tuple[StrategyInstance, DataPreparationResult]:
        entry = self._entry(release.release_id)
        if date.fromisoformat(str(entry["trading_date"])) != trading_date:
            raise AdviceClientError("requested trading date differs from prepared data")
        if entry.get("release_hash") != release.release_hash:
            raise AdviceClientError("prepared data belongs to another strategy release")
        relative = Path(str(entry.get("data_dir", "")))
        directory = (self.data_dir / relative).resolve()
        if relative.is_absolute() or not directory.is_relative_to(self.data_dir):
            raise AdviceClientError("prepared-data directory is unsafe")
        strategy = StrategyRuntime().create(
            StrategyInit(
                release,
                TradableWindow(trading_date, trading_date),
                directory,
                symbol=str(entry["symbol"]).upper(),
            )
        )
        prepared = strategy.prepare_data()
        if prepared.data_identity != entry.get("data_identity"):
            raise AdviceClientError("prepared-data identity differs from its index")
        return strategy, prepared

    def prepare_for_account(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        symbol: str,
        asset: str,
    ) -> DataPreparationResult:
        if asset != "etf":
            raise AdviceClientError("PTE currently requires one ETF strategy")
        release = self._load_release(strategy_id, strategy_version)
        trading_date = self.tradable_date(strategy_id, strategy_version)
        strategy, prepared = self._instance(release, trading_date)
        if strategy.identity.symbol != symbol.upper():
            raise AdviceClientError("SRT execution-pricing symbol differs from account")
        return prepared

    def price_history_for_account(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        symbol: str,
        asset: str,
    ) -> tuple[str, object]:
        if asset != "etf":
            raise AdviceClientError("PTE currently requires one ETF strategy")
        release = self._load_release(strategy_id, strategy_version)
        strategy, _ = self._instance(
            release, self.tradable_date(strategy_id, strategy_version)
        )
        if strategy.identity.symbol != symbol.upper():
            raise AdviceClientError("SRT execution-pricing symbol differs from account")
        frame = strategy.inspect_price_history()
        return canonical_frame_sha256(frame), frame

    def _audit_call(self, started: float, *, error=None, **scope) -> None:
        if self.audit is None or error is None:
            return
        self.audit.record(
            "DECISION_GENERATION_FAILED",
            source="srt_advice_client",
            outcome="FAILURE",
            actor_type="ENGINE",
            actor_id="strategy_runtime",
            correlation_id=f"srt:{scope.get('symbol')}",
            details={
                "service": "strategy_runtime",
                "operation": "plan_at",
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
        *,
        trading_date: date,
        portfolio_revision: int,
        state_revision: int,
        cycle_target_quantity: int | None = None,
        strategy_id: str | None = None,
        strategy_version: str | None = None,
        baseline: str | None = None,
        account_id: str | None = None,
        symbol: str | None = None,
        asset: str | None = None,
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
            strategy, _ = self._instance(release, trading_date)
            if strategy.definition.state_mode != "STATELESS":
                raise AdviceClientError("PTE does not support persisted SRT strategy state yet")
            if strategy.identity.symbol != selected_symbol:
                raise AdviceClientError("SRT strategy symbol differs from account")
            identity = _strategy_identity(self.repo_root, release)
            generated_at = self.now()
            generated_at = (
                generated_at.replace(tzinfo=_BEIJING)
                if generated_at.tzinfo is None
                else generated_at.astimezone(_BEIJING)
            )
            plan = strategy.plan_at(
                point=TradingPoint(trading_date, generated_at),
                portfolio=PortfolioSnapshot(
                    account_id,
                    selected_symbol,
                    Decimal(str(available_cash)),
                    Decimal(str(total_assets)),
                    int(actual_quantity),
                    portfolio_revision,
                    generated_at,
                ),
                state=ExecutionState(state_revision, generated_at, cycle_target_quantity),
            )
            decision = _decision_from_plan(plan, identity)
        except Exception as exc:
            self._audit_call(started, error=exc, **scope)
            if isinstance(exc, AdviceClientError):
                raise
            raise AdviceClientError(f"SRT decision generation failed: {exc}") from exc
        return decision
