from __future__ import annotations

import json

from czsc_trader.baselines import ResolvedBaseline, resolve_baseline

from .context import RepositoryContext
from .errors import ValidationError
from .results import CommandResult


def _metadata(resolved: ResolvedBaseline) -> dict[str, object]:
    return {
        "version": resolved.version,
        "sha256": resolved.sha256,
        "strategy": resolved.strategy,
        "status": resolved.status,
        "scope": resolved.scope,
        "symbol": resolved.symbol,
        "source_path": resolved.source_path,
        "source_sha256": resolved.source_sha256,
        "selection_sample_end": resolved.selection_sample_end,
        "forward_validation_start": resolved.forward_validation_start,
    }


def _strategy_identity(context: RepositoryContext, version: str) -> dict[str, object]:
    from strategy_manager import StrategyManagerError, StrategyRegistry

    try:
        registry = StrategyRegistry(context.strategy_root)
        release = registry.resolve_strategy(version)
        strategy = registry.get_strategy(release.strategy_id)
        return {
            "strategy_id": release.strategy_id,
            "strategy_name": strategy.name,
            "strategy_version": release.version,
            "release_hash": release.release_hash,
        }
    except StrategyManagerError:
        return {}


def list_baselines(context: RepositoryContext) -> CommandResult:
    path = context.baseline_root / "registry.json"
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
        baselines = registry["baselines"]
        rows = [
            {
                "version": version,
                "status": entry.get("status", "active"),
                "strategy": entry.get("strategy", "czsc_fixed_rule"),
                "scope": entry.get("scope", "generic"),
                "symbol": entry.get("symbol"),
                **_strategy_identity(context, version),
            }
            for version, entry in sorted(baselines.items())
        ]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "baseline_registry_invalid",
            f"cannot read baseline registry: {exc}",
            context={"path": str(path)},
        ) from exc
    return CommandResult(
        status="PASS",
        command="baseline.list",
        result={"latest": registry.get("latest"), "baselines": rows},
    )


def _resolve(
    context: RepositoryContext,
    version: str,
    *,
    symbol: str | None,
) -> ResolvedBaseline:
    try:
        return resolve_baseline(context.baseline_root, version, symbol=symbol)
    except (OSError, ValueError) as exc:
        raise ValidationError(
            "baseline_validation_failed",
            str(exc),
            context={"version": version, "symbol": symbol},
        ) from exc


def show_baseline(
    context: RepositoryContext,
    version: str,
    *,
    symbol: str | None = None,
) -> CommandResult:
    resolved = _resolve(context, version, symbol=symbol)
    return CommandResult(
        status="PASS",
        command="baseline.show",
        result={
            **_metadata(resolved),
            **_strategy_identity(context, version),
            "rule": resolved.rule_payload,
        },
    )


def validate_baseline(
    context: RepositoryContext,
    version: str,
    *,
    symbol: str | None = None,
) -> CommandResult:
    resolved = _resolve(context, version, symbol=symbol)
    return CommandResult(
        status="PASS",
        command="baseline.validate",
        result={**_metadata(resolved), **_strategy_identity(context, version)},
    )
