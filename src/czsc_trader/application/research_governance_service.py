"""TDR governance at the research-batch boundary.

Research remains flexible.  This service only creates and maintains the stable
StrategyFamily identity that a human explicitly approved.
"""

from __future__ import annotations

import json
import hashlib
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from strategy_manager import (
    ResearchState,
    StrategyFamily,
    StrategyManagerError,
    StrategyRegistry,
)

from .context import RepositoryContext
from .errors import ValidationError
from .results import CommandResult


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_object(context: RepositoryContext, path: Path) -> dict[str, Any]:
    resolved = path if path.is_absolute() else context.root / path
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {resolved}")
    return value


def _family_from_request(raw: dict[str, Any], *, actor: str) -> StrategyFamily:
    allowed = {"strategy_id", "name", "scope", "research_intent", "research_state"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"research batch request has unknown fields: {unknown}")
    missing = sorted({"strategy_id", "name", "scope", "research_intent"} - set(raw))
    if missing:
        raise ValueError(f"research batch request is missing fields: {missing}")
    timestamp = _now()
    return StrategyFamily.from_dict(
        {
            "schema_version": 2,
            "strategy_id": raw["strategy_id"],
            "name": raw["name"],
            "scope": raw["scope"],
            "research_intent": raw["research_intent"],
            "research_state": raw.get("research_state", ResearchState.RESEARCHING.value),
            "created_at": timestamp,
            "created_by": actor,
            "updated_at": timestamp,
        }
    )


def _handoff_text(family: StrategyFamily) -> str:
    scope = json.dumps(family.scope, ensure_ascii=False)
    intent = json.dumps(family.research_intent, ensure_ascii=False, indent=2)
    return (
        f"# {family.strategy_id} 研究交接\n\n"
        "## 当前身份\n\n"
        f"- 策略族：`{family.strategy_id} / {family.name}`；\n"
        f"- 初始范围：`{scope}`；\n"
        f"- 研究状态：`{family.research_state.value}`；\n"
        "- 当前没有候选、冻结版本或PTE账户。\n\n"
        "## 研究意图\n\n"
        "```json\n"
        f"{intent}\n"
        "```\n\n"
        "研究意图是可演化的人类语义。准确评价目标只在候选进入冻结流程时，"
        "通过EvaluationMandate正式确定。\n"
    )


def create_research_batch(
    context: RepositoryContext,
    input_path: Path,
    *,
    actor: str,
    reason: str,
) -> CommandResult:
    """Human gate 1: atomically establish family identity and research space."""

    try:
        family = _family_from_request(_read_object(context, input_path), actor=actor)
        destination = context.root / "research" / family.strategy_id
        if destination.exists():
            raise ValueError(f"research batch already exists: {family.strategy_id}")
        destination.mkdir(parents=True)
        registry = StrategyRegistry(context.strategy_root)
        try:
            handoff = destination / "HANDOFF.md"
            temporary_handoff = destination / ".HANDOFF.md.tmp"
            handoff_text = _handoff_text(family)
            temporary_handoff.write_text(handoff_text, encoding="utf-8", newline="\n")
            temporary_handoff.replace(handoff)
            credential_id = f"SGC-{family.strategy_id}-001"
            registry.create_family(
                family,
                actor=actor,
                reason=reason,
                credential_id=credential_id,
                credential_content={
                    "research_batch": family.to_dict(),
                    "reason": reason,
                },
                credential_artifact_hashes={
                    "research_handoff": hashlib.sha256(handoff_text.encode("utf-8")).hexdigest()
                },
            )
            credential = registry.get_governance_credential(family.strategy_id, credential_id)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
    except (StrategyManagerError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "research_batch_creation_failed",
            str(exc),
            context={"command": "research.create"},
        ) from exc
    return CommandResult(
        "PASS",
        "research.create",
        {
            "family": family.to_dict(),
            "governance_credential": {
                "credential_id": credential.credential_id,
                "stage": credential.stage.value,
                "result": credential.result.value,
                "credential_hash": credential.credential_hash,
            },
            "research_directory": str(destination.relative_to(context.root)),
        },
    )


def update_research_intent(
    context: RepositoryContext,
    strategy_id: str,
    input_path: Path,
    *,
    actor: str,
    reason: str,
) -> CommandResult:
    """Update flexible family-level intent without changing frozen versions."""

    try:
        raw = _read_object(context, input_path)
        allowed = {"research_intent", "research_state"}
        unknown = sorted(set(raw) - allowed)
        if unknown or not raw:
            raise ValueError(f"research intent update has invalid fields: {unknown or sorted(raw)}")
        family = StrategyRegistry(context.strategy_root).update_family(
            strategy_id,
            research_intent=raw.get("research_intent"),
            research_state=raw.get("research_state"),
            actor=actor,
            reason=reason,
        )
    except (StrategyManagerError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "research_intent_update_failed",
            str(exc),
            context={"command": "research.intent.update", "strategy_id": strategy_id},
        ) from exc
    return CommandResult("PASS", "research.intent.update", {"family": family.to_dict()})
