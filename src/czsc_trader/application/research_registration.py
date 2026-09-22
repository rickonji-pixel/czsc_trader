"""Move an approved research registration into strategy governance."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import json
from pathlib import Path
import shutil

from strategy_manager import (
    GovernanceResult,
    GovernanceStage,
    StrategyGovernanceCredential,
    StrategyRegistry,
)

from .context import RepositoryContext


_BACKFILL_FIELDS = {
    "schema_version",
    "strategy_id",
    "mode",
    "state",
    "backfilled_at",
    "credential_policy",
    "sources",
    "versions_at_backfill",
}


def _read_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _require_sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


def validate_registration_origin(
    context: RepositoryContext, strategy_id: str,
) -> dict | None:
    """Validate one immutable historical registration statement, when present."""

    path = context.research_registry_root / strategy_id / "registration_origin.json"
    if not path.is_file():
        return None
    origin = _read_object(path)
    if set(origin) != _BACKFILL_FIELDS or origin.get("schema_version") != 1:
        raise ValueError("historical registration origin fields are invalid")
    if (
        origin.get("strategy_id") != strategy_id
        or origin.get("mode") != "LEGACY_BACKFILL"
        or origin.get("state") != "PROMOTED_CLOSED"
        or origin.get("credential_policy") != "NO_RETROACTIVE_OPEN_CREDENTIAL"
    ):
        raise ValueError("historical registration origin identity is invalid")
    try:
        backfilled_at = datetime.fromisoformat(str(origin["backfilled_at"]))
    except ValueError as exc:
        raise ValueError("historical registration backfill time is invalid") from exc
    if backfilled_at.tzinfo is None:
        raise ValueError("historical registration backfill time must include a timezone")
    research = StrategyRegistry(context.research_registry_root)
    if research.get_family(strategy_id).strategy_id != strategy_id:
        raise ValueError("historical registration family identity is invalid")

    sources = origin.get("sources")
    expected_sources = {"governance_family", "governance_lifecycle", "research_handoff"}
    expected_paths = {
        "governance_family": f"strategies/{strategy_id}/family.json",
        "governance_lifecycle": f"strategies/{strategy_id}/lifecycle.jsonl",
        "research_handoff": f"research/{strategy_id}/HANDOFF.md",
    }
    if not isinstance(sources, dict) or set(sources) != expected_sources:
        raise ValueError("historical registration sources are invalid")
    for name, source in sources.items():
        if not isinstance(source, dict) or set(source) != {"path", "sha256"}:
            raise ValueError(f"historical registration source is invalid: {name}")
        if source["path"] != expected_paths[name]:
            raise ValueError(f"historical registration source path is invalid: {name}")
        if not (context.root / source["path"]).is_file():
            raise ValueError(f"historical registration source is unavailable: {name}")
        _require_sha256(source["sha256"], f"sources.{name}.sha256")

    versions = origin.get("versions_at_backfill")
    if not isinstance(versions, list) or not versions:
        raise ValueError("historical registration must identify frozen versions")
    governance = StrategyRegistry(context.strategy_root)
    seen: set[str] = set()
    for item in versions:
        if not isinstance(item, dict) or set(item) != {
            "strategy_version_id", "strategy_version_hash",
        }:
            raise ValueError("historical registration version identity is invalid")
        reference = item["strategy_version_id"]
        if not isinstance(reference, str) or reference in seen:
            raise ValueError("historical registration repeats a strategy version")
        family, separator, version = reference.rpartition("-")
        if not separator or family != strategy_id:
            raise ValueError("historical registration version belongs to another strategy")
        stored = governance.get_version(strategy_id, version)
        if stored.release_id != reference or stored.release_hash != _require_sha256(
            item["strategy_version_hash"], "strategy_version_hash"
        ):
            raise ValueError("historical registration differs from frozen strategy version")
        seen.add(reference)

    directory = context.research_registry_root / strategy_id / "credentials"
    if directory.is_dir():
        for credential_path in sorted(directory.glob("*.jsonl")):
            credential = research.get_governance_credential(
                strategy_id, credential_path.stem
            )
            if (
                credential.stage is GovernanceStage.RESEARCH_INITIATED
                and credential.result is GovernanceResult.OPEN
                and datetime.fromisoformat(credential.seals[0].occurred_at) <= backfilled_at
            ):
                raise ValueError(
                    "historical registration cannot expose a retroactive open credential"
                )
    return origin


@dataclass(frozen=True)
class RegistrationPromotion:
    """Filesystem state required to undo one new governance admission."""

    strategy_id: str
    credential_id: str
    changed: bool
    strategy_directory_existed: bool
    registry_before: bytes | None
    family_before: bytes | None
    lifecycle_before: bytes | None
    credential_before: bytes | None


def _read_if_file(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _restore(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.rollback.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def open_research_credential(
    context: RepositoryContext, strategy_id: str,
) -> StrategyGovernanceCredential:
    """Return the sole open pre-registration credential for one research family."""

    validate_registration_origin(context, strategy_id)
    registry = StrategyRegistry(context.research_registry_root)
    directory = context.research_registry_root / strategy_id / "credentials"
    credentials: list[StrategyGovernanceCredential] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.jsonl")):
            credential = registry.get_governance_credential(strategy_id, path.stem)
            if (
                credential.stage is GovernanceStage.RESEARCH_INITIATED
                and credential.result is GovernanceResult.OPEN
            ):
                credentials.append(credential)
    if len(credentials) != 1:
        raise ValueError(
            f"candidate review requires exactly one open research credential: {strategy_id}"
        )
    return credentials[0]


def promote_research_registration(
    context: RepositoryContext,
    strategy_id: str,
    credential_id: str,
) -> tuple[StrategyGovernanceCredential, RegistrationPromotion]:
    """Admit pre-registration identity into governance at candidate review time."""

    validate_registration_origin(context, strategy_id)
    research = StrategyRegistry(context.research_registry_root)
    family = research.get_family(strategy_id)
    source = research.get_governance_credential(strategy_id, credential_id)
    if (
        source.stage is not GovernanceStage.RESEARCH_INITIATED
        or source.result is not GovernanceResult.OPEN
    ):
        raise ValueError("research credential is not open for candidate review")
    first = source.seals[0]

    governance = StrategyRegistry(context.strategy_root)
    strategy_directory = context.strategy_root / strategy_id
    family_path = strategy_directory / "family.json"
    lifecycle_path = strategy_directory / "lifecycle.jsonl"
    credential_path = strategy_directory / "credentials" / f"{credential_id}.jsonl"
    promotion = RegistrationPromotion(
        strategy_id=strategy_id,
        credential_id=credential_id,
        changed=False,
        strategy_directory_existed=strategy_directory.exists(),
        registry_before=_read_if_file(governance.registry_path),
        family_before=_read_if_file(family_path),
        lifecycle_before=_read_if_file(lifecycle_path),
        credential_before=_read_if_file(credential_path),
    )

    if credential_path.is_file():
        existing = governance.get_governance_credential(strategy_id, credential_id)
        existing_first = existing.seals[0]
        if (
            existing_first.actor != first.actor
            or existing_first.content != first.content
            or existing_first.artifact_hashes != first.artifact_hashes
        ):
            raise ValueError("governance credential differs from research registration")
        return existing, promotion

    if family_path.is_file():
        governed_family = governance.get_family(strategy_id)
        if governed_family.name != family.name or governed_family.scope != family.scope:
            raise ValueError("governed strategy identity differs from research registration")
        _family, credential = governance.start_research_batch(
            strategy_id,
            research_intent=family.research_intent,
            research_state=family.research_state,
            actor=first.actor,
            reason="CANDIDATE_REVIEWED",
            credential_id=credential_id,
            credential_content=dict(first.content),
            credential_artifact_hashes=dict(first.artifact_hashes),
        )
    else:
        if strategy_directory.exists():
            raise ValueError("strategy governance directory exists without family identity")
        governance.create_family(
            family,
            actor=first.actor,
            reason="CANDIDATE_REVIEWED",
            credential_id=credential_id,
            credential_content=dict(first.content),
            credential_artifact_hashes=dict(first.artifact_hashes),
        )
        credential = governance.get_governance_credential(strategy_id, credential_id)

    return credential, replace(promotion, changed=True)


def rollback_research_promotion(
    context: RepositoryContext, promotion: RegistrationPromotion,
) -> None:
    """Undo a promotion when candidate admission fails later in the transaction."""

    if not promotion.changed:
        return
    strategy_directory = context.strategy_root / promotion.strategy_id
    if not promotion.strategy_directory_existed:
        shutil.rmtree(strategy_directory, ignore_errors=True)
    else:
        _restore(strategy_directory / "family.json", promotion.family_before)
        _restore(strategy_directory / "lifecycle.jsonl", promotion.lifecycle_before)
        _restore(
            strategy_directory / "credentials" / f"{promotion.credential_id}.jsonl",
            promotion.credential_before,
        )
    _restore(context.strategy_root / "registry.json", promotion.registry_before)
