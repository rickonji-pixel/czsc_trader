from __future__ import annotations

from typing import Any

from strategy_manager import StrategyRegistry, canonical_sha256

from czsc_trader.application.context import RepositoryContext
from czsc_trader.baselines import resolve_strategy_payload
from czsc_trader.identity import canonical_json_sha256

from .models import StrategyIdentity, StrategySnapshot


def _rule_symbol(strategy_payload: dict[str, object]) -> str:
    rule = strategy_payload.get("rule")
    execution = rule.get("execution") if isinstance(rule, dict) else None
    instrument = execution.get("instrument") if isinstance(execution, dict) else None
    symbol = instrument.get("symbol") if isinstance(instrument, dict) else None
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("strategy payload must define execution instrument symbol")
    return symbol.upper()


def _snapshot_hash(source_hash: str, resolved_rule: Any) -> str:
    return canonical_json_sha256(
        {
            "source_hash": source_hash,
            "rule": resolved_rule.rule_payload,
            "resolved_parent_sha256": resolved_rule.source_sha256,
        }
    )


def _resolve(
    context: RepositoryContext,
    identity: StrategyIdentity,
    strategy_payload: dict[str, object],
    source_hash: str,
) -> StrategySnapshot:
    symbol = _rule_symbol(strategy_payload)
    resolved = resolve_strategy_payload(
        context.strategy_dependency_root,
        strategy_payload,
        release_id=identity.reference,
        release_hash=source_hash,
        symbol=symbol,
        repository_root=context.root,
    )
    return StrategySnapshot(
        identity=identity,
        source_hash=source_hash,
        content_hash=_snapshot_hash(source_hash, resolved),
        strategy_payload=strategy_payload,
        resolved_rule=resolved,
    )


def resolve_registered_strategy(
    context: RepositoryContext,
    strategy_id: str,
    version: str,
) -> StrategySnapshot:
    registry = StrategyRegistry(context.strategy_root)
    release = registry.get_version(strategy_id, version)
    source_hash = release.release_hash or canonical_sha256(release.release_payload())
    return _resolve(
        context,
        StrategyIdentity("REGISTERED", release.release_id, str(context.strategy_root)),
        dict(release.strategy_payload),
        source_hash,
    )


def resolve_candidate_snapshot(
    context: RepositoryContext,
    candidate_id: str,
    strategy_payload: dict[str, object],
    content_hash: str,
    source: str,
) -> StrategySnapshot:
    if len(content_hash) != 64 or any(char not in "0123456789abcdef" for char in content_hash.lower()):
        raise ValueError("candidate content hash must be a lowercase SHA-256")
    return _resolve(
        context,
        StrategyIdentity("CANDIDATE", candidate_id, source),
        dict(strategy_payload),
        content_hash.lower(),
    )
