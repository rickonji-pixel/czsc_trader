"""Move an approved research registration into strategy governance."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import shutil

from strategy_manager import (
    GovernanceResult,
    GovernanceStage,
    StrategyGovernanceCredential,
    StrategyRegistry,
)

from .context import RepositoryContext


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
