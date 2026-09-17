from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from strategy_manager import (
    PerformanceEvidence,
    Strategy,
    StrategyManagerError,
    StrategyRegistry,
    StrategyVersion,
)

from .context import RepositoryContext
from .freeze_review import build_freeze_approval
from .errors import ValidationError
from .results import CommandResult
from .runtime_acceptance import require_runtime_readiness, validate_runtime_readiness


def _registry(context: RepositoryContext) -> StrategyRegistry:
    return StrategyRegistry(context.strategy_root)


def _domain_call(command: str, operation: Callable[[], Any]) -> Any:
    try:
        return operation()
    except (StrategyManagerError, OSError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "strategy_operation_failed",
            str(exc),
            context={"command": command},
        ) from exc


def _read_object(context: RepositoryContext, path: Path) -> dict[str, Any]:
    resolved = path if path.is_absolute() else context.root / path
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {resolved}")
    return value


def _identity(registry: StrategyRegistry, strategy_id: str, version: str) -> dict[str, Any]:
    strategy = registry.get_strategy(strategy_id)
    release = registry.get_version(strategy_id, version)
    return {
        "strategy_id": strategy.strategy_id,
        "name": strategy.name,
        "version": release.version,
        "release_id": release.release_id,
        "release_hash": release.release_hash,
        "qualification": registry.current_qualification(strategy_id, version).value,
    }


def list_strategies(context: RepositoryContext) -> CommandResult:
    registry = _registry(context)

    def operation() -> list[dict[str, Any]]:
        rows = []
        for strategy in registry.list_strategies():
            versions = registry.versions(strategy.strategy_id)
            rows.append(
                _identity(registry, strategy.strategy_id, versions[-1].version)
                if versions
                else {
                    "strategy_id": strategy.strategy_id,
                    "name": strategy.name,
                    "version": None,
                    "release_id": None,
                    "release_hash": None,
                    "qualification": None,
                }
            )
        return rows

    rows = _domain_call("strategy.list", operation)
    return CommandResult(status="PASS", command="strategy.list", result={"strategies": rows})


def show_strategy(
    context: RepositoryContext, reference: str, version: str | None = None
) -> CommandResult:
    registry = _registry(context)

    def operation() -> dict[str, Any]:
        strategy = registry.resolve_strategy_identity(reference)
        versions = registry.versions(strategy.strategy_id)
        if version is None and not versions:
            return {
                "strategy_id": strategy.strategy_id,
                "name": strategy.name,
                "version": None,
                "release_id": None,
                "release_hash": None,
                "qualification": None,
                "objective": strategy.objective,
                "responsibility": strategy.responsibility,
                "scope": strategy.scope,
            }
        release = registry.resolve_strategy(reference, version)
        strategy = registry.get_strategy(release.strategy_id)
        return {
            **_identity(registry, release.strategy_id, release.version),
            "objective": strategy.objective,
            "responsibility": strategy.responsibility,
            "scope": strategy.scope,
            "change_summary": release.change_summary,
            "source_experiment": release.source_experiment,
            "source_candidate": release.source_candidate,
            "selection_data_cutoff": release.selection_data_cutoff,
            "forward_start": release.forward_start,
            "strategy_payload": release.strategy_payload,
        }

    result = _domain_call("strategy.show", operation)
    return CommandResult(status="PASS", command="strategy.show", result=result)


def strategy_history(context: RepositoryContext, strategy_id: str) -> CommandResult:
    registry = _registry(context)
    events = _domain_call(
        "strategy.history",
        lambda: [item.to_dict() for item in registry.lifecycle_events(strategy_id)],
    )
    return CommandResult(
        status="PASS", command="strategy.history", result={"strategy_id": strategy_id, "events": events}
    )


def strategy_performance(
    context: RepositoryContext, strategy_id: str, version: str | None = None
) -> CommandResult:
    registry = _registry(context)

    def operation() -> dict[str, Any]:
        grouped = {"RESEARCH_BACKTEST": [], "PAPER_FORWARD": [], "LIVE": []}
        for item in registry.evidence(strategy_id, version):
            grouped[item.phase.value].append(item.to_dict())
        return {"strategy_id": strategy_id, "version": version, "phases": grouped}

    result = _domain_call("strategy.performance", operation)
    return CommandResult(status="PASS", command="strategy.performance", result=result)


def validate_strategies(context: RepositoryContext) -> CommandResult:
    result = _domain_call("strategy.validate", lambda: _registry(context).validate_all())
    return CommandResult(status="PASS", command="strategy.validate", result=result)


def create_strategy(
    context: RepositoryContext, input_path: Path, *, actor: str, reason: str
) -> CommandResult:
    registry = _registry(context)

    def operation() -> dict[str, Any]:
        strategy = Strategy.from_dict(_read_object(context, input_path))
        return registry.create_strategy(strategy, actor=actor, reason=reason).to_dict()

    result = _domain_call("strategy.create", operation)
    return CommandResult(status="PASS", command="strategy.create", result=result)


def create_strategy_version(
    context: RepositoryContext, input_path: Path, *, actor: str, reason: str
) -> CommandResult:
    registry = _registry(context)

    def operation() -> dict[str, Any]:
        version = StrategyVersion.from_dict(_read_object(context, input_path))
        created, event = registry.create_version(version, actor=actor, reason=reason)
        return {"version": created.to_dict(), "event": event.to_dict()}

    result = _domain_call("strategy.version.create", operation)
    return CommandResult(status="PASS", command="strategy.version.create", result=result)


def freeze_strategy_version(
    context: RepositoryContext,
    strategy_id: str,
    version: str,
    evidence_path: Path,
    health_check_path: Path,
    *,
    actor: str,
    reason: str,
) -> CommandResult:
    registry = _registry(context)

    def operation() -> dict[str, Any]:
        document = _read_object(context, health_check_path)
        machine_report = document.get("machine_report")
        review = document.get("review")
        if not isinstance(machine_report, dict) or not isinstance(review, dict):
            raise ValueError("health check must contain machine_report and review objects")
        source_experiment = registry.get_version(
            strategy_id, version
        ).source_experiment
        source_root = (context.root / source_experiment).resolve()
        experiments_root = context.experiments_root.resolve()
        if source_root != experiments_root and experiments_root not in source_root.parents:
            raise ValueError("strategy source experiment is outside the experiment repository")
        canonical_report_path = source_root / "artifacts" / "machine_evaluation.json"
        if not canonical_report_path.is_file():
            raise ValueError("strategy source experiment has no SE machine report")
        if machine_report != _read_object(context, canonical_report_path):
            raise ValueError("health check machine report differs from source experiment")
        approval = build_freeze_approval(
            machine_report=machine_report,
            review=review,
            strategy_id=strategy_id,
            source_experiment=source_experiment,
            actor=actor,
            reason=reason,
        )
        runtime_acceptance = require_runtime_readiness(
            registry.get_version(strategy_id, version), validate_runtime_readiness
        )
        frozen, event = registry.freeze_version(
            strategy_id,
            version,
            actor=actor,
            reason=reason,
            evidence=_read_object(context, evidence_path),
            approval=approval,
            machine_report=machine_report,
        )
        return {
            "version": frozen.to_dict(),
            "event": event.to_dict(),
            "runtime_acceptance": runtime_acceptance,
        }

    result = _domain_call("strategy.freeze", operation)
    return CommandResult(status="PASS", command="strategy.freeze", result=result)


def transition_strategy_version(
    context: RepositoryContext,
    action: str,
    strategy_id: str,
    version: str,
    *,
    actor: str,
    reason: str,
    evidence_ids: list[str] | None = None,
) -> CommandResult:
    registry = _registry(context)

    def operation() -> dict[str, Any]:
        if action == "promote":
            event = registry.promote_version(
                strategy_id,
                version,
                actor=actor,
                reason=reason,
                evidence_ids=evidence_ids or [],
            )
        elif action == "downgrade":
            event = registry.downgrade_version(
                strategy_id,
                version,
                actor=actor,
                reason=reason,
                evidence_ids=evidence_ids or [],
            )
        elif action == "retire":
            event = registry.retire_version(
                strategy_id, version, actor=actor, reason=reason
            )
        else:
            raise ValueError(f"unknown strategy transition: {action}")
        return event.to_dict()

    command = f"strategy.{action}"
    result = _domain_call(command, operation)
    return CommandResult(status="PASS", command=command, result=result)


def add_strategy_evidence(context: RepositoryContext, input_path: Path) -> CommandResult:
    registry = _registry(context)

    def operation() -> dict[str, Any]:
        resolved = input_path if input_path.is_absolute() else context.root / input_path
        document = _read_object(context, input_path)
        raw_evidence = document.get("evidence", document)
        if not isinstance(raw_evidence, dict):
            raise ValueError("evidence bundle must contain an evidence object")
        evidence = PerformanceEvidence.from_dict(raw_evidence)
        source_file = resolved if "source" in document else None
        return registry.record_evidence(evidence, source_file=source_file).to_dict()

    result = _domain_call("strategy.evidence.add", operation)
    return CommandResult(status="PASS", command="strategy.evidence.add", result=result)
