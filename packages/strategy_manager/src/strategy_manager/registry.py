from __future__ import annotations

import json
import shutil
import unicodedata
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import (
    EvidenceRequiredError,
    ImmutableVersionError,
    InvalidTransitionError,
    RegistryError,
    ValidationError,
)
from .lifecycle import qualification_is_deployable, validate_transition
from .models import (
    AdjudicationReport,
    CandidateSnapshot,
    EvidencePhase,
    EvaluationMandate,
    FreezeApproval,
    FreezeReviewCase,
    GovernanceResult,
    GovernanceStage,
    LifecycleEvent,
    PerformanceEvidence,
    Qualification,
    ResearchState,
    ReviewStatus,
    StrategyFamily,
    StrategyGovernanceCredential,
    StrategyGovernanceSeal,
    StrategyVersion,
    canonical_sha256,
)
from .validation import require_string, require_timestamp
from .write_lock import RegistryWriteLock, registry_write


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalized_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _canonical_json(value: Any, *, newline: bool = True) -> str:
    suffix = "\n" if newline else ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + suffix


class StrategyRegistry:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self._write_lock = RegistryWriteLock(self.root)

    @property
    def registry_path(self) -> Path:
        return self.root / "registry.json"

    def _load_registry(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return {"schema_version": 1, "strategies": []}
        value = self._read_json(self.registry_path)
        if set(value) != {"schema_version", "strategies"} or value["schema_version"] != 1:
            raise RegistryError("registry.json has an unsupported schema")
        if not isinstance(value["strategies"], list):
            raise RegistryError("registry strategies must be a list")
        return value

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RegistryError(f"cannot read JSON file: {path}") from exc
        if not isinstance(value, dict):
            raise RegistryError(f"JSON root must be an object: {path}")
        return value

    @registry_write
    def _atomic_write(self, path: Path, text: str, expected: bytes | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        current = path.read_bytes() if path.exists() else None
        if current != expected:
            raise RegistryError(f"concurrent change detected: {path}")
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(text, encoding="utf-8", newline="\n")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @registry_write
    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
        expected = path.read_bytes() if path.exists() else None
        self._atomic_write(path, _canonical_json(value), expected)

    @registry_write
    def _append_jsonl(self, path: Path, value: dict[str, Any]) -> None:
        expected = path.read_bytes() if path.exists() else None
        existing = expected.decode("utf-8") if expected else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        line = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self._atomic_write(path, f"{existing}{line}\n", expected)

    @registry_write
    def _restore_files(self, snapshots: list[tuple[Path, bytes | None]]) -> None:
        """Best-effort compensation for a failed multi-file governance commit."""

        failures: list[str] = []
        for path, previous in reversed(snapshots):
            try:
                if previous is None:
                    if path.exists():
                        path.unlink()
                else:
                    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.rollback")
                    temporary.write_bytes(previous)
                    temporary.replace(path)
            except OSError as exc:
                failures.append(f"{path}: {exc}")
        if failures:
            raise RegistryError("governance transaction rollback failed: " + "; ".join(failures))

    def _strategy_dir(self, strategy_id: str) -> Path:
        return self.root / strategy_id

    def _version_path(self, strategy_id: str, version: str) -> Path:
        return self._strategy_dir(strategy_id) / "versions" / f"{version}.json"

    def _review_dir(self, strategy_id: str, review_id: str) -> Path:
        return self._strategy_dir(strategy_id) / "reviews" / review_id

    def _credential_path(self, strategy_id: str, credential_id: str) -> Path:
        return self._strategy_dir(strategy_id) / "credentials" / f"{credential_id}.jsonl"

    @staticmethod
    def _build_governance_seal(
        *,
        credential_id: str,
        strategy_id: str,
        sequence: int,
        stage: GovernanceStage,
        result: GovernanceResult,
        actor: str,
        previous_seal_hash: str | None,
        content: dict[str, Any],
        artifact_hashes: dict[str, str],
    ) -> StrategyGovernanceSeal:
        payload = {
            "schema_version": 1,
            "credential_id": credential_id,
            "strategy_id": strategy_id,
            "sequence": sequence,
            "stage": stage.value,
            "result": result.value,
            "actor": actor,
            "occurred_at": _now(),
            "previous_seal_hash": previous_seal_hash,
            "content": content,
            "content_hash": canonical_sha256(content),
            "artifact_hashes": artifact_hashes,
        }
        return StrategyGovernanceSeal.from_dict({**payload, "seal_hash": canonical_sha256(payload)})

    def get_governance_credential(
        self, strategy_id: str, credential_id: str
    ) -> StrategyGovernanceCredential:
        path = self._credential_path(strategy_id, credential_id)
        if not path.is_file():
            raise RegistryError(f"unknown governance credential: {credential_id}")
        try:
            seals = tuple(
                StrategyGovernanceSeal.from_dict(json.loads(line))
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            return StrategyGovernanceCredential.from_seals(seals)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise RegistryError(f"invalid governance credential: {credential_id}") from exc

    @registry_write
    def open_governance_credential(
        self,
        credential_id: str,
        strategy_id: str,
        *,
        actor: str,
        content: dict[str, Any],
        artifact_hashes: dict[str, str] | None = None,
    ) -> StrategyGovernanceCredential:
        """Create the first seal for one globally unique governance credential."""

        self.get_family(strategy_id)
        path = self._credential_path(strategy_id, credential_id)
        if path.exists():
            existing = self.get_governance_credential(strategy_id, credential_id)
            first = existing.seals[0]
            if (
                first.actor == actor
                and first.content == content
                and first.artifact_hashes == (artifact_hashes or {})
            ):
                return existing
            raise RegistryError(f"governance credential already exists: {credential_id}")
        for family in self.list_families():
            other = self._credential_path(family.strategy_id, credential_id)
            if other.exists():
                raise RegistryError(
                    f"governance credential id already belongs to {family.strategy_id}: "
                    f"{credential_id}"
                )
        seal = self._build_governance_seal(
            credential_id=credential_id,
            strategy_id=strategy_id,
            sequence=1,
            stage=GovernanceStage.RESEARCH_INITIATED,
            result=GovernanceResult.OPEN,
            actor=actor,
            previous_seal_hash=None,
            content=content,
            artifact_hashes=artifact_hashes or {},
        )
        self._append_jsonl(path, seal.to_dict())
        return StrategyGovernanceCredential.from_seals((seal,))

    @registry_write
    def append_governance_seal(
        self,
        strategy_id: str,
        credential_id: str,
        *,
        stage: GovernanceStage,
        result: GovernanceResult,
        actor: str,
        expected_previous_hash: str,
        content: dict[str, Any],
        artifact_hashes: dict[str, str] | None = None,
    ) -> StrategyGovernanceCredential:
        """Append one seal after validating the complete existing hash chain."""

        try:
            stage = GovernanceStage(stage)
            result = GovernanceResult(result)
        except (TypeError, ValueError) as exc:
            raise RegistryError("unsupported governance seal stage or result") from exc
        credential = self.get_governance_credential(strategy_id, credential_id)
        artifacts = artifact_hashes or {}
        last = credential.seals[-1]
        if (
            last.stage is stage
            and last.result is result
            and last.actor == actor
            and last.content == content
            and last.artifact_hashes == artifacts
            and last.previous_seal_hash == expected_previous_hash
        ):
            return credential
        if credential.credential_hash != expected_previous_hash:
            raise RegistryError(
                f"stale governance credential hash: expected {credential.credential_hash}"
            )
        seal = self._build_governance_seal(
            credential_id=credential_id,
            strategy_id=strategy_id,
            sequence=len(credential.seals) + 1,
            stage=stage,
            result=result,
            actor=actor,
            previous_seal_hash=credential.credential_hash,
            content=content,
            artifact_hashes=artifacts,
        )
        updated = StrategyGovernanceCredential.from_seals((*credential.seals, seal))
        self._append_jsonl(self._credential_path(strategy_id, credential_id), seal.to_dict())
        return updated

    def list_families(self) -> list[StrategyFamily]:
        return [
            self.get_family(item["strategy_id"]) for item in self._load_registry()["strategies"]
        ]

    def get_family(self, strategy_id: str) -> StrategyFamily:
        path = self._strategy_dir(strategy_id) / "family.json"
        if not path.exists():
            raise RegistryError(f"unknown strategy family: {strategy_id}")
        return StrategyFamily.from_dict(self._read_json(path))

    def resolve_family(self, reference: str) -> StrategyFamily:
        registry = self._load_registry()
        match = next(
            (
                item
                for item in registry["strategies"]
                if reference == item["strategy_id"] or reference in item.get("aliases", [])
            ),
            None,
        )
        if match is None:
            raise RegistryError(f"unknown strategy reference: {reference}")
        return self.get_family(match["strategy_id"])

    def get_version(self, strategy_id: str, version: str) -> StrategyVersion:
        path = self._version_path(strategy_id, version)
        if not path.exists():
            raise RegistryError(f"unknown strategy version: {strategy_id}-{version}")
        raw = self._read_json(path)
        try:
            model = StrategyVersion.from_dict(raw)
        except ValidationError as exc:
            if "release_hash does not match" in str(exc):
                raise ImmutableVersionError(
                    f"release hash mismatch: {strategy_id}-{version}"
                ) from exc
            raise RegistryError(f"invalid strategy version {strategy_id}-{version}: {exc}") from exc
        return model

    def _versions(self, strategy_id: str) -> list[StrategyVersion]:
        directory = self._strategy_dir(strategy_id) / "versions"
        if not directory.exists():
            return []
        versions = [self.get_version(strategy_id, path.stem) for path in directory.glob("v*.json")]
        return sorted(versions, key=lambda item: int(item.version[1:]))

    def versions(self, strategy_id: str) -> tuple[StrategyVersion, ...]:
        """Return registered versions, including an empty tuple for a new identity."""
        self.get_family(strategy_id)
        return tuple(self._versions(strategy_id))

    def resolve_strategy(self, reference: str, version: str | None = None) -> StrategyVersion:
        strategy = self.resolve_family(reference)
        versions = self._versions(strategy.strategy_id)
        if version is not None:
            return self.get_version(strategy.strategy_id, version)
        if not versions:
            raise RegistryError(f"strategy has no versions: {strategy.strategy_id}")
        return versions[-1]

    @registry_write
    def create_family(
        self,
        strategy: StrategyFamily | dict[str, Any],
        *,
        actor: str,
        reason: str,
        aliases: list[str] | None = None,
        credential_id: str | None = None,
        credential_content: dict[str, Any] | None = None,
        credential_artifact_hashes: dict[str, str] | None = None,
    ) -> StrategyFamily:
        require_string(actor, "actor")
        require_string(reason, "reason")
        model = (
            strategy if isinstance(strategy, StrategyFamily) else StrategyFamily.from_dict(strategy)
        )
        registry = self._load_registry()
        if any(item["strategy_id"] == model.strategy_id for item in registry["strategies"]):
            raise RegistryError(f"strategy already exists: {model.strategy_id}")
        normalized = _normalized_name(model.name)
        if any(_normalized_name(existing.name) == normalized for existing in self.list_families()):
            raise RegistryError(f"active strategy name already exists: {model.name}")
        alias_list = aliases or []
        known_references = {
            reference
            for item in registry["strategies"]
            for reference in [item["strategy_id"], *item.get("aliases", [])]
        }
        if model.strategy_id in alias_list or any(
            alias in known_references for alias in alias_list
        ):
            raise RegistryError("strategy aliases must be unique")
        registry["strategies"].append(
            {"strategy_id": model.strategy_id, "path": model.strategy_id, "aliases": alias_list}
        )
        registry["strategies"].sort(key=lambda item: item["strategy_id"])
        strategy_dir = self._strategy_dir(model.strategy_id)
        strategy_path = strategy_dir / "family.json"
        if strategy_dir.exists():
            raise RegistryError(f"strategy directory already exists: {model.strategy_id}")
        registry_before = self.registry_path.read_bytes() if self.registry_path.exists() else None
        event = self._event(
            "RESEARCH_BATCH_CREATED",
            model.strategy_id,
            None,
            None,
            Qualification.RESEARCH,
            actor,
            reason,
            [f"FAMILY:{canonical_sha256(model.to_dict())}"],
            None,
        )
        credential_path: Path | None = None
        credential_text: str | None = None
        if credential_id is not None:
            credential_id = require_string(credential_id, "credential_id")
            if not isinstance(credential_content, dict) or not credential_content:
                raise RegistryError(
                    "credential_content is required when creating a governance credential"
                )
            for family in self.list_families():
                if self._credential_path(family.strategy_id, credential_id).exists():
                    raise RegistryError(f"governance credential id already exists: {credential_id}")
            seal = self._build_governance_seal(
                credential_id=credential_id,
                strategy_id=model.strategy_id,
                sequence=1,
                stage=GovernanceStage.RESEARCH_INITIATED,
                result=GovernanceResult.OPEN,
                actor=actor,
                previous_seal_hash=None,
                content=credential_content,
                artifact_hashes=credential_artifact_hashes or {},
            )
            credential_path = self._credential_path(model.strategy_id, credential_id)
            credential_text = (
                json.dumps(
                    seal.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
        try:
            self._atomic_write(strategy_path, _canonical_json(model.to_dict()), None)
            self._append_jsonl(strategy_dir / "lifecycle.jsonl", event.to_dict())
            if credential_path is not None and credential_text is not None:
                self._atomic_write(credential_path, credential_text, None)
            self._atomic_write(
                self.registry_path,
                _canonical_json(registry),
                registry_before,
            )
        except Exception:
            shutil.rmtree(strategy_dir, ignore_errors=True)
            raise
        return model

    @registry_write
    def start_research_batch(
        self,
        strategy_id: str,
        *,
        research_intent: dict[str, Any],
        research_state: ResearchState | str,
        actor: str,
        reason: str,
        credential_id: str,
        credential_content: dict[str, Any],
        credential_artifact_hashes: dict[str, str] | None = None,
    ) -> tuple[StrategyFamily, StrategyGovernanceCredential]:
        """Atomically start another governed research batch for an existing family."""

        actor = require_string(actor, "actor")
        reason = require_string(reason, "reason")
        credential_id = require_string(credential_id, "credential_id")
        if not isinstance(research_intent, dict) or not research_intent:
            raise ValidationError("research_intent must be a nonempty JSON object")
        try:
            next_state = (
                research_state
                if isinstance(research_state, ResearchState)
                else ResearchState(research_state)
            )
        except ValueError as exc:
            raise ValidationError("research_state has unsupported value") from exc
        if next_state is ResearchState.TERMINATED:
            raise ValidationError("a new research batch cannot start as TERMINATED")
        if not isinstance(credential_content, dict) or not credential_content:
            raise ValidationError("credential_content must be a nonempty object")

        current = self.get_family(strategy_id)
        credential_path = self._credential_path(strategy_id, credential_id)
        if credential_path.exists():
            existing = self.get_governance_credential(strategy_id, credential_id)
            first = existing.seals[0]
            if (
                first.actor == actor
                and first.content == credential_content
                and first.artifact_hashes == (credential_artifact_hashes or {})
            ):
                return current, existing
            raise RegistryError(f"governance credential already exists: {credential_id}")
        for family in self.list_families():
            if self._credential_path(family.strategy_id, credential_id).exists():
                raise RegistryError(
                    f"governance credential id already belongs to {family.strategy_id}: "
                    f"{credential_id}"
                )

        updated = replace(
            current,
            research_intent=dict(research_intent),
            research_state=next_state,
            updated_at=_now(),
        )
        seal = self._build_governance_seal(
            credential_id=credential_id,
            strategy_id=strategy_id,
            sequence=1,
            stage=GovernanceStage.RESEARCH_INITIATED,
            result=GovernanceResult.OPEN,
            actor=actor,
            previous_seal_hash=None,
            content=credential_content,
            artifact_hashes=credential_artifact_hashes or {},
        )
        event = self._event(
            "RESEARCH_BATCH_CREATED",
            strategy_id,
            None,
            None,
            Qualification.RESEARCH,
            actor,
            reason,
            [credential_id, f"FAMILY:{canonical_sha256(updated.to_dict())}"],
            None,
        )
        family_path = self._strategy_dir(strategy_id) / "family.json"
        lifecycle_path = self._strategy_dir(strategy_id) / "lifecycle.jsonl"
        family_before = family_path.read_bytes()
        lifecycle_before = lifecycle_path.read_bytes() if lifecycle_path.exists() else None
        lifecycle_text = lifecycle_before.decode("utf-8") if lifecycle_before else ""
        snapshots = [
            (family_path, family_before),
            (lifecycle_path, lifecycle_before),
            (credential_path, None),
        ]
        try:
            self._atomic_write(family_path, _canonical_json(updated.to_dict()), family_before)
            self._atomic_write(
                lifecycle_path,
                lifecycle_text
                + json.dumps(
                    event.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                lifecycle_before,
            )
            self._atomic_write(
                credential_path,
                json.dumps(
                    seal.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                None,
            )
        except Exception:
            self._restore_files(snapshots)
            raise
        return updated, StrategyGovernanceCredential.from_seals((seal,))

    @registry_write
    def update_family(
        self,
        strategy_id: str,
        *,
        research_intent: dict[str, Any] | None = None,
        research_state: ResearchState | str | None = None,
        actor: str,
        reason: str,
    ) -> StrategyFamily:
        actor = require_string(actor, "actor")
        reason = require_string(reason, "reason")
        current = self.get_family(strategy_id)
        next_intent = current.research_intent if research_intent is None else research_intent
        if not isinstance(next_intent, dict) or not next_intent:
            raise ValidationError("research_intent must be a nonempty JSON object")
        next_state = current.research_state
        if research_state is not None:
            try:
                next_state = (
                    research_state
                    if isinstance(research_state, ResearchState)
                    else ResearchState(research_state)
                )
            except ValueError as exc:
                raise ValidationError("research_state has unsupported value") from exc
        updated = replace(
            current,
            research_intent=dict(next_intent),
            research_state=next_state,
            updated_at=_now(),
        )
        if updated == current:
            return current
        event = self._event(
            "RESEARCH_INTENT_UPDATED",
            strategy_id,
            None,
            None,
            Qualification.RESEARCH,
            actor,
            reason,
            [f"FAMILY:{canonical_sha256(updated.to_dict())}"],
            None,
        )
        family_path = self._strategy_dir(strategy_id) / "family.json"
        lifecycle_path = self._strategy_dir(strategy_id) / "lifecycle.jsonl"
        family_before = family_path.read_bytes()
        lifecycle_before = lifecycle_path.read_bytes() if lifecycle_path.exists() else None
        try:
            self._atomic_write(family_path, _canonical_json(updated.to_dict()), family_before)
            self._append_jsonl(lifecycle_path, event.to_dict())
        except Exception:
            self._atomic_write(
                family_path,
                family_before.decode("utf-8"),
                family_path.read_bytes() if family_path.exists() else None,
            )
            if lifecycle_before is None:
                lifecycle_path.unlink(missing_ok=True)
            elif lifecycle_path.exists() and lifecycle_path.read_bytes() != lifecycle_before:
                self._atomic_write(
                    lifecycle_path,
                    lifecycle_before.decode("utf-8"),
                    lifecycle_path.read_bytes(),
                )
            raise
        return updated

    @registry_write
    def _legacy_open_freeze_review(
        self,
        review_id: str,
        candidate: CandidateSnapshot | dict[str, Any],
        mandate: EvaluationMandate | dict[str, Any],
        *,
        actor: str,
    ) -> FreezeReviewCase:
        actor = require_string(actor, "actor")
        snapshot = (
            candidate
            if isinstance(candidate, CandidateSnapshot)
            else CandidateSnapshot.from_dict(candidate)
        )
        evaluation_mandate = (
            mandate
            if isinstance(mandate, EvaluationMandate)
            else EvaluationMandate.from_dict(mandate)
        )
        self.get_family(snapshot.strategy_id)
        if (
            evaluation_mandate.strategy_id != snapshot.strategy_id
            or evaluation_mandate.candidate_id != snapshot.candidate_id
        ):
            raise RegistryError("candidate snapshot and evaluation mandate identity differ")
        review_id = require_string(review_id, "review_id")
        audit_policy_hash = canonical_sha256(
            {
                "policy_version": "tdr-freeze-v1",
                "required_audits": evaluation_mandate.required_audits,
            }
        )
        now = _now()
        case = FreezeReviewCase.from_dict(
            {
                "schema_version": 1,
                "review_id": review_id,
                "strategy_id": snapshot.strategy_id,
                "candidate_id": snapshot.candidate_id,
                "candidate_hash": snapshot.candidate_hash,
                "candidate_snapshot_hash": canonical_sha256(snapshot.to_dict()),
                "evaluation_mandate_hash": evaluation_mandate.mandate_hash,
                "audit_policy_hash": audit_policy_hash,
                "status": ReviewStatus.OPEN.value,
                "adjudication_report_hash": None,
                "opened_at": now,
                "opened_by": actor,
                "updated_at": now,
                "invalidation_reason": None,
            }
        )
        destination = self._review_dir(snapshot.strategy_id, review_id)
        documents = {
            "candidate_snapshot.json": snapshot.to_dict(),
            "evaluation_mandate.json": evaluation_mandate.to_dict(),
            "review_case.json": case.to_dict(),
        }
        if destination.exists():
            existing = {name: self._read_json(destination / name) for name in documents}
            if existing == documents:
                return case
            raise RegistryError(f"freeze review already exists: {review_id}")
        destination.mkdir(parents=True)
        try:
            for name, document in documents.items():
                self._atomic_write(
                    destination / name,
                    _canonical_json(document),
                    None,
                )
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        return case

    def _legacy_get_freeze_review(
        self, strategy_id: str, review_id: str
    ) -> tuple[FreezeReviewCase, CandidateSnapshot, EvaluationMandate]:
        directory = self._review_dir(strategy_id, review_id)
        if not directory.is_dir():
            raise RegistryError(f"unknown freeze review: {review_id}")
        case = FreezeReviewCase.from_dict(self._read_json(directory / "review_case.json"))
        snapshot = CandidateSnapshot.from_dict(
            self._read_json(directory / "candidate_snapshot.json")
        )
        mandate = EvaluationMandate.from_dict(
            self._read_json(directory / "evaluation_mandate.json")
        )
        if case.strategy_id != strategy_id:
            raise RegistryError("freeze review belongs to another strategy family")
        if case.candidate_snapshot_hash != canonical_sha256(snapshot.to_dict()):
            raise RegistryError("freeze review candidate snapshot hash mismatch")
        if case.evaluation_mandate_hash != mandate.mandate_hash:
            raise RegistryError("freeze review evaluation mandate hash mismatch")
        expected_policy = canonical_sha256(
            {"policy_version": "tdr-freeze-v1", "required_audits": mandate.required_audits}
        )
        if case.audit_policy_hash != expected_policy:
            raise RegistryError("freeze review audit policy hash mismatch")
        return case, snapshot, mandate

    @registry_write
    def _legacy_record_adjudication(
        self,
        report: AdjudicationReport | dict[str, Any],
    ) -> FreezeReviewCase:
        model = (
            report
            if isinstance(report, AdjudicationReport)
            else AdjudicationReport.from_dict(report)
        )
        case, snapshot, mandate = self._legacy_get_freeze_review(model.strategy_id, model.review_id)
        if case.status in {ReviewStatus.INVALIDATED, ReviewStatus.FROZEN}:
            raise RegistryError(f"freeze review cannot be evaluated: {case.status.value}")
        if (
            model.candidate_id != snapshot.candidate_id
            or model.candidate_hash != snapshot.candidate_hash
            or model.evaluation_mandate_hash != mandate.mandate_hash
            or model.audit_policy_hash != case.audit_policy_hash
        ):
            raise RegistryError("adjudication report identity differs from freeze review")
        missing = sorted(set(mandate.required_audits) - model.audit_results.keys())
        if missing and model.machine_verdict != "INCOMPLETE":
            raise RegistryError("adjudication report omits required audits")
        status = {
            "ELIGIBLE_FOR_FREEZE_REVIEW": ReviewStatus.ELIGIBLE,
            "INCOMPLETE": ReviewStatus.INCOMPLETE,
            "REJECTED": ReviewStatus.REJECTED,
        }[model.machine_verdict]
        updated = replace(
            case,
            status=status,
            adjudication_report_hash=model.report_hash,
            updated_at=_now(),
        )
        directory = self._review_dir(model.strategy_id, model.review_id)
        report_path = directory / "adjudication_report.json"
        case_path = directory / "review_case.json"
        snapshots = [
            (report_path, report_path.read_bytes() if report_path.exists() else None),
            (case_path, case_path.read_bytes() if case_path.exists() else None),
        ]
        if report_path.exists():
            existing = AdjudicationReport.from_dict(self._read_json(report_path))
            if existing != model:
                raise RegistryError("freeze review already has another adjudication report")
        try:
            if not report_path.exists():
                self._atomic_write(report_path, _canonical_json(model.to_dict()), None)
            self._write_json(case_path, updated.to_dict())
        except Exception:
            self._restore_files(snapshots)
            raise
        return updated

    @registry_write
    def _legacy_invalidate_freeze_review(
        self, strategy_id: str, review_id: str, *, reason: str
    ) -> FreezeReviewCase:
        reason = require_string(reason, "reason")
        case, _snapshot, _mandate = self._legacy_get_freeze_review(strategy_id, review_id)
        if case.status is ReviewStatus.FROZEN:
            raise RegistryError("frozen review cannot be invalidated")
        updated = replace(
            case,
            status=ReviewStatus.INVALIDATED,
            updated_at=_now(),
            invalidation_reason=reason,
        )
        self._write_json(
            self._review_dir(strategy_id, review_id) / "review_case.json",
            updated.to_dict(),
        )
        return updated

    @registry_write
    def _legacy_create_version(
        self,
        version: StrategyVersion | dict[str, Any],
        *,
        actor: str,
        reason: str,
    ) -> tuple[StrategyVersion, LifecycleEvent]:
        actor = require_string(actor, "actor")
        reason = require_string(reason, "reason")
        model = (
            version if isinstance(version, StrategyVersion) else StrategyVersion.from_dict(version)
        )
        self.get_family(model.strategy_id)
        versions = self._versions(model.strategy_id)
        next_number = len(versions) + 1
        expected_version = f"v{next_number}"
        expected_parent = None if not versions else versions[-1].version
        if model.version != expected_version:
            raise RegistryError(f"next version must be {expected_version}")
        if model.parent_version != expected_parent:
            raise RegistryError(f"parent_version must be {expected_parent!r}")
        if model.release_hash is not None:
            raise RegistryError("new strategy versions must start in RESEARCH")
        event = self._event(
            "VERSION_CREATED",
            model.strategy_id,
            model.version,
            None,
            Qualification.RESEARCH,
            actor,
            reason,
            [],
            None,
        )
        self._atomic_write(
            self._version_path(model.strategy_id, model.version),
            _canonical_json(model.to_dict()),
            None,
        )
        self._append_jsonl(
            self._strategy_dir(model.strategy_id) / "lifecycle.jsonl", event.to_dict()
        )
        return model, event

    @registry_write
    def _legacy_create_frozen_version(
        self,
        strategy_id: str,
        review_id: str,
        *,
        actor: str,
        reason: str,
        human_decision: dict[str, Any],
        runtime_acceptance: dict[str, Any],
        evidence: dict[str, Any],
        change_summary: str,
    ) -> tuple[StrategyVersion, LifecycleEvent]:
        actor = require_string(actor, "actor")
        reason = require_string(reason, "reason")
        change_summary = require_string(change_summary, "change_summary")
        case, snapshot, mandate = self._legacy_get_freeze_review(strategy_id, review_id)
        if case.status is ReviewStatus.FROZEN:
            matching = [
                item
                for item in self._versions(strategy_id)
                if item.schema_version == 2
                and item.governance is not None
                and item.governance.get("review_id") == review_id
            ]
            if len(matching) != 1:
                raise RegistryError("frozen review does not resolve to exactly one version")
            existing = matching[0]
            decision = self._read_json(
                self._review_dir(strategy_id, review_id) / "human_decision.json"
            )
            if (
                decision.get("actor") != actor
                or decision.get("reason") != reason
                or existing.change_summary != change_summary
            ):
                raise RegistryError("freeze review already completed with different input")
            events = [
                item
                for item in self.lifecycle_events(strategy_id)
                if item.version == existing.version
                and item.event_type == "VERSION_FROZEN"
                and review_id in item.evidence_ids
            ]
            if len(events) != 1:
                raise RegistryError("frozen review lifecycle event is inconsistent")
            return existing, events[0]
        if case.status is not ReviewStatus.ELIGIBLE:
            raise EvidenceRequiredError(f"freeze review is not eligible: {case.status.value}")
        report_path = self._review_dir(strategy_id, review_id) / "adjudication_report.json"
        if not report_path.is_file():
            raise EvidenceRequiredError("freeze review has no adjudication report")
        report = AdjudicationReport.from_dict(self._read_json(report_path))
        if report.report_hash != case.adjudication_report_hash:
            raise EvidenceRequiredError("freeze review report hash mismatch")
        required_decision = {
            "schema_version",
            "review_id",
            "strategy_id",
            "candidate_id",
            "candidate_hash",
            "decision",
            "actor",
            "reason",
            "decided_at",
            "decision_hash",
        }
        if set(human_decision) != required_decision:
            raise EvidenceRequiredError("human freeze decision fields are incomplete")
        decision_payload = dict(human_decision)
        decision_hash = decision_payload.pop("decision_hash", None)
        if decision_hash != canonical_sha256(decision_payload):
            raise EvidenceRequiredError("human freeze decision hash mismatch")
        if (
            human_decision["schema_version"] != 1
            or human_decision["review_id"] != review_id
            or human_decision["strategy_id"] != strategy_id
            or human_decision["candidate_id"] != snapshot.candidate_id
            or human_decision["candidate_hash"] != snapshot.candidate_hash
            or human_decision["decision"] != "APPROVE_FREEZE"
            or human_decision["actor"] != actor
            or human_decision["reason"] != reason
        ):
            raise EvidenceRequiredError("human freeze decision does not match the review")
        require_timestamp(human_decision["decided_at"], "decided_at")
        if not isinstance(runtime_acceptance, dict) or runtime_acceptance.get("status") != "PASS":
            raise EvidenceRequiredError("runtime acceptance must PASS before freeze")
        if (
            runtime_acceptance.get("release_id")
            != f"{strategy_id}-v{len(self._versions(strategy_id)) + 1}"
        ):
            raise EvidenceRequiredError("runtime acceptance belongs to another release")
        if runtime_acceptance.get("strategy_payload_hash") != canonical_sha256(
            snapshot.strategy_payload
        ):
            raise EvidenceRequiredError("runtime acceptance belongs to another strategy payload")
        runtime_sha256 = runtime_acceptance.get("runtime_sha256")
        if not isinstance(runtime_sha256, str) or len(runtime_sha256) != 64:
            raise EvidenceRequiredError("runtime acceptance is missing runtime identity")
        runtime_acceptance_hash = canonical_sha256(runtime_acceptance)
        versions = self._versions(strategy_id)
        number = len(versions) + 1
        version_name = f"v{number}"
        parent = versions[-1].version if versions else None
        governance = {
            "review_id": review_id,
            "candidate_snapshot_hash": case.candidate_snapshot_hash,
            "evaluation_mandate_hash": case.evaluation_mandate_hash,
            "adjudication_report_hash": report.report_hash,
            "human_decision_hash": str(decision_hash),
            "runtime_acceptance_hash": runtime_acceptance_hash,
        }
        governance_hash = canonical_sha256(governance)
        draft = StrategyVersion.from_dict(
            {
                "schema_version": 2,
                "strategy_id": strategy_id,
                "version": version_name,
                "release_id": f"{strategy_id}-{version_name}",
                "parent_version": parent,
                "change_summary": change_summary,
                "source_experiment": snapshot.source_experiment,
                "source_candidate": snapshot.candidate_id,
                "selection_data_cutoff": mandate.development_cutoff,
                "forward_start": mandate.forward_start,
                "strategy_payload": snapshot.strategy_payload,
                "release_hash": None,
                "governance": governance,
                "governance_hash": governance_hash,
            }
        )
        frozen = replace(draft, release_hash=canonical_sha256(draft.release_payload()))
        if runtime_acceptance.get("release_hash") != frozen.release_hash:
            raise EvidenceRequiredError(
                "runtime acceptance release hash differs from the frozen release"
            )
        incoming = dict(evidence)
        incoming.update(
            {
                "strategy_id": strategy_id,
                "version": version_name,
                "release_hash": frozen.release_hash,
            }
        )
        research_evidence = PerformanceEvidence.from_dict(incoming)
        if research_evidence.phase is not EvidencePhase.RESEARCH_BACKTEST:
            raise EvidenceRequiredError("freeze requires RESEARCH_BACKTEST evidence")
        event = self._event(
            "VERSION_FROZEN",
            strategy_id,
            version_name,
            None,
            Qualification.PAPER_READY,
            actor,
            reason,
            [
                research_evidence.evidence_id,
                review_id,
                f"DECISION:{decision_hash}",
                f"RUNTIME:{runtime_acceptance_hash}",
            ],
            frozen.release_hash,
        )
        version_path = self._version_path(strategy_id, version_name)
        if version_path.exists():
            raise RegistryError(f"strategy version already exists: {frozen.release_id}")
        evidence_path = self._strategy_dir(strategy_id) / "evidence.jsonl"
        lifecycle_path = self._strategy_dir(strategy_id) / "lifecycle.jsonl"
        expected_evidence = evidence_path.read_bytes() if evidence_path.exists() else None
        expected_lifecycle = lifecycle_path.read_bytes() if lifecycle_path.exists() else None
        evidence_text = expected_evidence.decode("utf-8") if expected_evidence else ""
        lifecycle_text = expected_lifecycle.decode("utf-8") if expected_lifecycle else ""
        evidence_line = json.dumps(
            research_evidence.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        event_line = json.dumps(
            event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        review_dir = self._review_dir(strategy_id, review_id)
        decision_path = review_dir / "human_decision.json"
        runtime_path = review_dir / "runtime_acceptance.json"
        if decision_path.exists() or runtime_path.exists():
            raise RegistryError("freeze review already contains a freeze decision")
        frozen_case = replace(case, status=ReviewStatus.FROZEN, updated_at=_now())
        case_path = review_dir / "review_case.json"
        snapshots = [
            (version_path, None),
            (evidence_path, expected_evidence),
            (lifecycle_path, expected_lifecycle),
            (decision_path, None),
            (runtime_path, None),
            (case_path, case_path.read_bytes()),
        ]
        try:
            self._atomic_write(version_path, _canonical_json(frozen.to_dict()), None)
            self._atomic_write(
                evidence_path, f"{evidence_text}{evidence_line}\n", expected_evidence
            )
            self._atomic_write(
                lifecycle_path, f"{lifecycle_text}{event_line}\n", expected_lifecycle
            )
            self._atomic_write(decision_path, _canonical_json(human_decision), None)
            self._atomic_write(runtime_path, _canonical_json(runtime_acceptance), None)
            self._write_json(case_path, frozen_case.to_dict())
        except Exception:
            self._restore_files(snapshots)
            raise
        return frozen, event

    @registry_write
    def create_frozen_version_from_credential(
        self,
        strategy_id: str,
        credential_id: str,
        *,
        actor: str,
        reason: str,
        evidence: dict[str, Any],
        change_summary: str,
    ) -> tuple[StrategyVersion, LifecycleEvent, StrategyGovernanceCredential]:
        """Atomically freeze the version and stamp the existing governance credential."""

        actor = require_string(actor, "actor")
        reason = require_string(reason, "reason")
        change_summary = require_string(change_summary, "change_summary")
        credential = self.get_governance_credential(strategy_id, credential_id)
        if credential.stage is GovernanceStage.VERSION_FROZEN:
            matching = [
                item
                for item in self._versions(strategy_id)
                if item.schema_version == 3
                and item.governance is not None
                and item.governance.get("credential_id") == credential_id
            ]
            if len(matching) != 1:
                raise RegistryError(
                    "frozen governance credential does not resolve to exactly one version"
                )
            version = matching[0]
            approval = next(
                (
                    seal
                    for seal in reversed(credential.seals)
                    if seal.stage is GovernanceStage.FREEZE_APPROVED
                ),
                None,
            )
            decision = approval.content.get("human_decision") if approval is not None else None
            if (
                not isinstance(decision, dict)
                or decision.get("actor") != actor
                or decision.get("reason") != reason
                or version.change_summary != change_summary
            ):
                raise RegistryError("governance credential already froze with different input")
            events = [
                item
                for item in self.lifecycle_events(strategy_id)
                if item.version == version.version
                and item.event_type == "VERSION_FROZEN"
                and credential_id in item.evidence_ids
            ]
            if len(events) != 1:
                raise RegistryError("frozen governance lifecycle event is inconsistent")
            return version, events[0], credential
        if credential.stage is not GovernanceStage.FREEZE_APPROVED:
            raise EvidenceRequiredError(
                f"governance credential is not approved: {credential.stage.value}"
            )

        submission = next(
            (
                seal
                for seal in reversed(credential.seals)
                if seal.stage is GovernanceStage.CANDIDATE_SUBMITTED
            ),
            None,
        )
        adjudication = next(
            (
                seal
                for seal in reversed(credential.seals)
                if seal.stage is GovernanceStage.TDR_ADJUDICATED
            ),
            None,
        )
        approval = credential.seals[-1]
        if submission is None or adjudication is None:
            raise EvidenceRequiredError(
                "governance credential is missing submission or adjudication"
            )
        try:
            snapshot = CandidateSnapshot.from_dict(dict(submission.content["candidate_snapshot"]))
            mandate = EvaluationMandate.from_dict(dict(submission.content["evaluation_mandate"]))
            report = AdjudicationReport.from_dict(dict(adjudication.content["adjudication_report"]))
            human_decision = dict(approval.content["human_decision"])
            runtime_acceptance = dict(approval.content["runtime_acceptance"])
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise EvidenceRequiredError(
                "governance credential content is incomplete or invalid"
            ) from exc
        if (
            snapshot.strategy_id != strategy_id
            or mandate.strategy_id != strategy_id
            or report.strategy_id != strategy_id
            or mandate.candidate_id != snapshot.candidate_id
            or report.candidate_id != snapshot.candidate_id
            or report.candidate_hash != snapshot.candidate_hash
            or report.evaluation_mandate_hash != mandate.mandate_hash
            or report.machine_verdict != "ELIGIBLE_FOR_FREEZE_REVIEW"
            or adjudication.result is not GovernanceResult.ELIGIBLE
        ):
            raise EvidenceRequiredError("governance credential identities or verdict differ")
        if (
            human_decision.get("credential_id") != credential_id
            or human_decision.get("strategy_id") != strategy_id
            or human_decision.get("candidate_id") != snapshot.candidate_id
            or human_decision.get("candidate_hash") != snapshot.candidate_hash
            or human_decision.get("decision") != "APPROVE_FREEZE"
            or human_decision.get("actor") != actor
            or human_decision.get("reason") != reason
        ):
            raise EvidenceRequiredError("human freeze decision does not match credential")
        require_timestamp(human_decision.get("decided_at"), "decided_at")
        if runtime_acceptance.get("status") != "PASS":
            raise EvidenceRequiredError("runtime acceptance must PASS before freeze")
        if runtime_acceptance.get("strategy_payload_hash") != canonical_sha256(
            snapshot.strategy_payload
        ):
            raise EvidenceRequiredError("runtime acceptance belongs to another strategy payload")
        runtime_sha256 = runtime_acceptance.get("runtime_sha256")
        if not isinstance(runtime_sha256, str) or len(runtime_sha256) != 64:
            raise EvidenceRequiredError("runtime acceptance is missing runtime identity")

        versions = self._versions(strategy_id)
        version_name = f"v{len(versions) + 1}"
        release_id = f"{strategy_id}-{version_name}"
        if runtime_acceptance.get("release_id") != release_id:
            raise EvidenceRequiredError("runtime acceptance belongs to another release")
        governance = {
            "credential_id": credential_id,
            "candidate_submission_seal_hash": submission.seal_hash,
            "adjudication_seal_hash": adjudication.seal_hash,
            "approval_seal_hash": approval.seal_hash,
            "candidate_snapshot_hash": canonical_sha256(snapshot.to_dict()),
            "evaluation_mandate_hash": mandate.mandate_hash,
            "adjudication_report_hash": report.report_hash,
        }
        draft = StrategyVersion.from_dict(
            {
                "schema_version": 3,
                "strategy_id": strategy_id,
                "version": version_name,
                "release_id": release_id,
                "parent_version": versions[-1].version if versions else None,
                "change_summary": change_summary,
                "source_experiment": snapshot.source_experiment,
                "source_candidate": (
                    snapshot.candidate_id
                    if snapshot.candidate_id.startswith(f"{strategy_id}-")
                    else f"{strategy_id}-{snapshot.candidate_id}"
                ),
                "selection_data_cutoff": mandate.development_cutoff,
                "forward_start": mandate.forward_start,
                "strategy_payload": snapshot.strategy_payload,
                "release_hash": None,
                "governance": governance,
                "governance_hash": canonical_sha256(governance),
            }
        )
        frozen = replace(draft, release_hash=canonical_sha256(draft.release_payload()))
        if runtime_acceptance.get("release_hash") != frozen.release_hash:
            raise EvidenceRequiredError(
                "runtime acceptance release hash differs from the frozen release"
            )
        incoming = dict(evidence)
        incoming.update(
            {
                "strategy_id": strategy_id,
                "version": version_name,
                "release_hash": frozen.release_hash,
            }
        )
        research_evidence = PerformanceEvidence.from_dict(incoming)
        if research_evidence.phase is not EvidencePhase.RESEARCH_BACKTEST:
            raise EvidenceRequiredError("freeze requires RESEARCH_BACKTEST evidence")
        event = self._event(
            "VERSION_FROZEN",
            strategy_id,
            version_name,
            None,
            Qualification.PAPER_READY,
            actor,
            reason,
            [
                research_evidence.evidence_id,
                credential_id,
                f"APPROVAL_SEAL:{approval.seal_hash}",
            ],
            frozen.release_hash,
        )
        final_content = {
            "release_id": release_id,
            "release_hash": frozen.release_hash,
            "version_record_hash": canonical_sha256(frozen.to_dict()),
            "evidence_id": research_evidence.evidence_id,
            "lifecycle_event_id": event.event_id,
        }
        final_seal = self._build_governance_seal(
            credential_id=credential_id,
            strategy_id=strategy_id,
            sequence=len(credential.seals) + 1,
            stage=GovernanceStage.VERSION_FROZEN,
            result=GovernanceResult.FROZEN,
            actor=actor,
            previous_seal_hash=credential.credential_hash,
            content=final_content,
            artifact_hashes={
                "strategy_version": canonical_sha256(frozen.to_dict()),
                "research_evidence": canonical_sha256(research_evidence.to_dict()),
            },
        )
        updated_credential = StrategyGovernanceCredential.from_seals(
            (*credential.seals, final_seal)
        )

        version_path = self._version_path(strategy_id, version_name)
        if version_path.exists():
            raise RegistryError(f"strategy version already exists: {release_id}")
        evidence_path = self._strategy_dir(strategy_id) / "evidence.jsonl"
        lifecycle_path = self._strategy_dir(strategy_id) / "lifecycle.jsonl"
        credential_path = self._credential_path(strategy_id, credential_id)
        before_evidence = evidence_path.read_bytes() if evidence_path.exists() else None
        before_lifecycle = lifecycle_path.read_bytes() if lifecycle_path.exists() else None
        before_credential = credential_path.read_bytes()
        evidence_text = before_evidence.decode("utf-8") if before_evidence else ""
        lifecycle_text = before_lifecycle.decode("utf-8") if before_lifecycle else ""
        credential_text = before_credential.decode("utf-8")
        snapshots = [
            (version_path, None),
            (evidence_path, before_evidence),
            (lifecycle_path, before_lifecycle),
            (credential_path, before_credential),
        ]
        try:
            self._atomic_write(version_path, _canonical_json(frozen.to_dict()), None)
            self._atomic_write(
                evidence_path,
                evidence_text
                + json.dumps(
                    research_evidence.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                before_evidence,
            )
            self._atomic_write(
                lifecycle_path,
                lifecycle_text
                + json.dumps(
                    event.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                before_lifecycle,
            )
            self._atomic_write(
                credential_path,
                credential_text
                + json.dumps(
                    final_seal.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                before_credential,
            )
        except Exception:
            self._restore_files(snapshots)
            raise
        return frozen, event, updated_credential

    @registry_write
    def record_legacy_governance_acceptance(
        self,
        strategy_id: str,
        version: str,
        *,
        actor: str,
        reason: str,
        cutover_commit: str,
    ) -> LifecycleEvent:
        actor = require_string(actor, "actor")
        reason = require_string(reason, "reason")
        cutover_commit = require_string(cutover_commit, "cutover_commit")
        current = self.current_qualification(strategy_id, version)
        existing = [
            item
            for item in self.lifecycle_events(strategy_id)
            if item.version == version and item.event_type == "LEGACY_GOVERNANCE_ACCEPTED"
        ]
        if existing:
            return existing[-1]
        release = self.get_version(strategy_id, version)
        event = self._event(
            "LEGACY_GOVERNANCE_ACCEPTED",
            strategy_id,
            version,
            current,
            current,
            actor,
            reason,
            ["TDR_GOVERNANCE_V2", f"CUTOVER:{cutover_commit}"],
            release.release_hash,
        )
        self._append_jsonl(self._strategy_dir(strategy_id) / "lifecycle.jsonl", event.to_dict())
        return event

    def lifecycle_events(self, strategy_id: str) -> list[LifecycleEvent]:
        path = self._strategy_dir(strategy_id) / "lifecycle.jsonl"
        if not path.exists():
            return []
        try:
            return [
                LifecycleEvent.from_dict(json.loads(line))
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (json.JSONDecodeError, ValidationError) as exc:
            raise RegistryError(f"invalid lifecycle history: {strategy_id}") from exc

    def current_qualification(self, strategy_id: str, version: str) -> Qualification:
        events = [event for event in self.lifecycle_events(strategy_id) if event.version == version]
        if not events:
            raise RegistryError(f"strategy version has no lifecycle: {strategy_id}-{version}")
        return events[-1].to_state

    def evidence(self, strategy_id: str, version: str | None = None) -> list[PerformanceEvidence]:
        path = self._strategy_dir(strategy_id) / "evidence.jsonl"
        if not path.exists():
            return []
        try:
            values = [
                PerformanceEvidence.from_dict(json.loads(line))
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (json.JSONDecodeError, ValidationError) as exc:
            raise RegistryError(f"invalid evidence history: {strategy_id}") from exc
        return values if version is None else [item for item in values if item.version == version]

    @registry_write
    def record_evidence(
        self, evidence: PerformanceEvidence | dict[str, Any], *, source_file: Path | None = None
    ) -> PerformanceEvidence:
        model = (
            evidence
            if isinstance(evidence, PerformanceEvidence)
            else PerformanceEvidence.from_dict(evidence)
        )
        version = self.get_version(model.strategy_id, model.version)
        if not version.release_hash or model.release_hash != version.release_hash:
            raise RegistryError("evidence release_hash does not match the frozen version")
        existing = {item.evidence_id: item for item in self.evidence(model.strategy_id)}
        if model.evidence_id in existing:
            if existing[model.evidence_id] == model:
                return model
            raise RegistryError(f"evidence_id already exists: {model.evidence_id}")
        if source_file is not None:
            source = Path(source_file)
            document = json.loads(source.read_text(encoding="utf-8"))
            source_payload = document.get("source") if isinstance(document, dict) else None
            if not isinstance(source_payload, dict):
                raise RegistryError("source evidence bundle is missing its source object")
            if canonical_sha256(source_payload) != model.source_hash:
                raise RegistryError("source evidence hash mismatch")
            destination = (
                self._strategy_dir(model.strategy_id) / "evidence" / f"{model.evidence_id}.json"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and destination.read_bytes() != source.read_bytes():
                raise RegistryError(f"evidence artifact already exists: {model.evidence_id}")
            if not destination.exists():
                temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
                shutil.copyfile(source, temporary)
                temporary.replace(destination)
        self._append_jsonl(
            self._strategy_dir(model.strategy_id) / "evidence.jsonl", model.to_dict()
        )
        return model

    @registry_write
    def _legacy_freeze_version(
        self,
        strategy_id: str,
        version: str,
        *,
        actor: str,
        reason: str,
        evidence: PerformanceEvidence | dict[str, Any],
        approval: FreezeApproval | dict[str, Any],
        machine_report: dict[str, Any],
    ) -> tuple[StrategyVersion, LifecycleEvent]:
        actor = require_string(actor, "actor")
        reason = require_string(reason, "reason")
        current = self.current_qualification(strategy_id, version)
        validate_transition(current, Qualification.PAPER_READY)
        research = self.get_version(strategy_id, version)
        if research.release_hash is not None:
            raise ImmutableVersionError(
                f"strategy version is already frozen: {research.release_id}"
            )
        release_hash = canonical_sha256(research.release_payload())
        frozen = replace(research, release_hash=release_hash)
        freeze_approval = (
            approval
            if isinstance(approval, FreezeApproval)
            else FreezeApproval.from_dict(dict(approval))
        )
        report = dict(machine_report)
        report_hash = report.pop("report_hash", None)
        if report_hash is None or report_hash != canonical_sha256(report):
            raise EvidenceRequiredError("freeze machine report hash mismatch")
        required_report = {
            "schema_version",
            "report_id",
            "candidate_id",
            "candidate_hash",
            "machine_verdict",
            "risk_label",
        }
        if not required_report <= report.keys() or report.get("schema_version") != 2:
            raise EvidenceRequiredError("freeze requires a schema v2 machine report")
        if report.get("machine_verdict") != "ELIGIBLE_FOR_FREEZE_REVIEW":
            raise EvidenceRequiredError("freeze machine report is not eligible for review")
        if (
            freeze_approval.machine_report_id != report.get("report_id")
            or freeze_approval.machine_report_hash != report_hash
            or freeze_approval.machine_verdict != report.get("machine_verdict")
        ):
            raise EvidenceRequiredError("freeze approval and machine report differ")
        if freeze_approval.strategy_id != strategy_id:
            raise EvidenceRequiredError("freeze approval belongs to another strategy")
        if str(research.source_candidate) != freeze_approval.candidate_id:
            raise EvidenceRequiredError("freeze approval belongs to another candidate")
        if (
            freeze_approval.candidate_id != report.get("candidate_id")
            or freeze_approval.candidate_hash != report.get("candidate_hash")
            or freeze_approval.risk_label != report.get("risk_label")
        ):
            raise EvidenceRequiredError("freeze candidate and machine report differ")
        candidate_source = research.strategy_payload.get("candidate_source", {})
        payload_hash = (
            candidate_source.get("candidate_hash") if isinstance(candidate_source, dict) else None
        )
        if payload_hash is not None and payload_hash != freeze_approval.candidate_hash:
            raise EvidenceRequiredError("freeze approval candidate hash mismatch")
        incoming = (
            evidence.to_dict() if isinstance(evidence, PerformanceEvidence) else dict(evidence)
        )
        incoming.update(
            {"strategy_id": strategy_id, "version": version, "release_hash": release_hash}
        )
        research_evidence = PerformanceEvidence.from_dict(incoming)
        if research_evidence.phase is not EvidencePhase.RESEARCH_BACKTEST:
            raise EvidenceRequiredError("freeze requires RESEARCH_BACKTEST evidence")
        if any(
            item.evidence_id == research_evidence.evidence_id for item in self.evidence(strategy_id)
        ):
            raise RegistryError(f"evidence_id already exists: {research_evidence.evidence_id}")
        event = self._event(
            "VERSION_FROZEN",
            strategy_id,
            version,
            current,
            Qualification.PAPER_READY,
            actor,
            reason,
            [research_evidence.evidence_id, freeze_approval.assessment_id],
            release_hash,
        )
        version_path = self._version_path(strategy_id, version)
        expected_version = version_path.read_bytes()
        evidence_path = self._strategy_dir(strategy_id) / "evidence.jsonl"
        lifecycle_path = self._strategy_dir(strategy_id) / "lifecycle.jsonl"
        approval_path = self._strategy_dir(strategy_id) / "freeze_approvals.jsonl"
        expected_evidence = evidence_path.read_bytes() if evidence_path.exists() else None
        expected_lifecycle = lifecycle_path.read_bytes() if lifecycle_path.exists() else None
        expected_approval = approval_path.read_bytes() if approval_path.exists() else None
        self._atomic_write(version_path, _canonical_json(frozen.to_dict()), expected_version)
        existing_evidence = expected_evidence.decode("utf-8") if expected_evidence else ""
        evidence_line = json.dumps(
            research_evidence.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        self._atomic_write(
            evidence_path,
            f"{existing_evidence}{evidence_line}\n",
            expected_evidence,
        )
        existing_approvals = expected_approval.decode("utf-8") if expected_approval else ""
        approval_line = json.dumps(
            freeze_approval.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        self._atomic_write(
            approval_path,
            f"{existing_approvals}{approval_line}\n",
            expected_approval,
        )
        existing_lifecycle = expected_lifecycle.decode("utf-8") if expected_lifecycle else ""
        event_line = json.dumps(
            event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        self._atomic_write(
            lifecycle_path,
            f"{existing_lifecycle}{event_line}\n",
            expected_lifecycle,
        )
        return frozen, event

    @registry_write
    def promote_version(
        self,
        strategy_id: str,
        version: str,
        *,
        actor: str,
        reason: str,
        evidence_ids: list[str],
    ) -> LifecycleEvent:
        return self._transition(
            strategy_id,
            version,
            Qualification.LIVE_READY,
            "VERSION_PROMOTED",
            actor,
            reason,
            evidence_ids,
            required_phase=EvidencePhase.PAPER_FORWARD,
        )

    @registry_write
    def downgrade_version(
        self,
        strategy_id: str,
        version: str,
        *,
        actor: str,
        reason: str,
        evidence_ids: list[str],
    ) -> LifecycleEvent:
        if not evidence_ids:
            raise EvidenceRequiredError("downgrade requires related evidence")
        return self._transition(
            strategy_id,
            version,
            Qualification.PAPER_READY,
            "VERSION_DOWNGRADED",
            actor,
            reason,
            evidence_ids,
        )

    @registry_write
    def retire_version(
        self, strategy_id: str, version: str, *, actor: str, reason: str
    ) -> LifecycleEvent:
        return self._transition(
            strategy_id,
            version,
            Qualification.RETIRED,
            "VERSION_RETIRED",
            actor,
            reason,
            [],
        )

    @registry_write
    def _transition(
        self,
        strategy_id: str,
        version: str,
        target: Qualification,
        event_type: str,
        actor: str,
        reason: str,
        evidence_ids: list[str],
        required_phase: EvidencePhase | None = None,
    ) -> LifecycleEvent:
        actor = require_string(actor, "actor")
        reason = require_string(reason, "reason")
        current = self.current_qualification(strategy_id, version)
        validate_transition(current, target)
        registered = {item.evidence_id: item for item in self.evidence(strategy_id, version)}
        if any(evidence_id not in registered for evidence_id in evidence_ids):
            raise RegistryError("lifecycle transition references unknown evidence")
        if required_phase is not None and not any(
            registered[evidence_id].phase is required_phase for evidence_id in evidence_ids
        ):
            raise EvidenceRequiredError(f"transition requires {required_phase.value} evidence")
        frozen = self.get_version(strategy_id, version)
        event = self._event(
            event_type,
            strategy_id,
            version,
            current,
            target,
            actor,
            reason,
            evidence_ids,
            frozen.release_hash,
        )
        self._append_jsonl(self._strategy_dir(strategy_id) / "lifecycle.jsonl", event.to_dict())
        return event

    @staticmethod
    def _event(
        event_type: str,
        strategy_id: str,
        version: str,
        from_state: Qualification | None,
        to_state: Qualification,
        actor: str,
        reason: str,
        evidence_ids: list[str],
        release_hash: str | None,
    ) -> LifecycleEvent:
        return LifecycleEvent.from_dict(
            {
                "schema_version": 1,
                "event_id": f"EVT-{uuid.uuid4().hex}",
                "event_type": event_type,
                "strategy_id": strategy_id,
                "version": version,
                "from_state": None if from_state is None else from_state.value,
                "to_state": to_state.value,
                "occurred_at": _now(),
                "actor": actor,
                "reason": reason,
                "evidence_ids": evidence_ids,
                "release_hash": release_hash,
            }
        )

    def assert_deployable(
        self, strategy_id: str, version: str, environment: str
    ) -> StrategyVersion:
        qualification = self.current_qualification(strategy_id, version)
        if not qualification_is_deployable(qualification, environment):
            raise InvalidTransitionError(
                f"{strategy_id}-{version} is not deployable to {environment}: {qualification.value}"
            )
        release = self.get_version(strategy_id, version)
        self.validate_version_governance(strategy_id, version)
        return release

    def validate_version_governance(self, strategy_id: str, version: str) -> str:
        """Prove that one frozen release has a valid current or legacy governance identity."""

        release = self.get_version(strategy_id, version)
        if not release.release_hash:
            raise EvidenceRequiredError("strategy version is not frozen")
        if release.schema_version != 3:
            accepted = [
                item
                for item in self.lifecycle_events(strategy_id)
                if item.version == version
                and item.event_type == "LEGACY_GOVERNANCE_ACCEPTED"
                and item.release_hash == release.release_hash
                and "TDR_GOVERNANCE_V2" in item.evidence_ids
            ]
            if len(accepted) != 1:
                raise EvidenceRequiredError(
                    f"legacy strategy version has no unique governance acceptance: "
                    f"{release.release_id}"
                )
            return "LEGACY_GOVERNANCE_ACCEPTED"

        governance = release.governance
        if not isinstance(governance, dict):
            raise EvidenceRequiredError("strategy version has no governance identity")
        credential_id = governance.get("credential_id")
        if not isinstance(credential_id, str):
            raise EvidenceRequiredError("strategy version has no governance credential id")
        credential = self.get_governance_credential(strategy_id, credential_id)
        if credential.stage is not GovernanceStage.VERSION_FROZEN:
            raise EvidenceRequiredError("strategy governance credential is not frozen")
        final = credential.seals[-1]
        submission = next(
            seal
            for seal in reversed(credential.seals)
            if seal.stage is GovernanceStage.CANDIDATE_SUBMITTED
        )
        adjudication = next(
            seal
            for seal in reversed(credential.seals)
            if seal.stage is GovernanceStage.TDR_ADJUDICATED
        )
        approval = next(
            seal
            for seal in reversed(credential.seals)
            if seal.stage is GovernanceStage.FREEZE_APPROVED
        )
        snapshot = CandidateSnapshot.from_dict(
            dict(submission.content["candidate_snapshot"])
        )
        mandate = EvaluationMandate.from_dict(
            dict(submission.content["evaluation_mandate"])
        )
        report = AdjudicationReport.from_dict(
            dict(adjudication.content["adjudication_report"])
        )
        expected_governance = {
            "credential_id": credential_id,
            "candidate_submission_seal_hash": submission.seal_hash,
            "adjudication_seal_hash": adjudication.seal_hash,
            "approval_seal_hash": approval.seal_hash,
            "candidate_snapshot_hash": canonical_sha256(snapshot.to_dict()),
            "evaluation_mandate_hash": mandate.mandate_hash,
            "adjudication_report_hash": report.report_hash,
        }
        evidence_id = final.content.get("evidence_id")
        matching_evidence = [
            item for item in self.evidence(strategy_id, version)
            if item.evidence_id == evidence_id
        ]
        events = [
            item for item in self.lifecycle_events(strategy_id)
            if item.version == version
            and item.event_type == "VERSION_FROZEN"
            and credential_id in item.evidence_ids
            and item.release_hash == release.release_hash
        ]
        if (
            final.content.get("release_id") != release.release_id
            or final.content.get("release_hash") != release.release_hash
            or final.content.get("version_record_hash")
            != canonical_sha256(release.to_dict())
            or release.governance != expected_governance
            or len(matching_evidence) != 1
            or len(events) != 1
            or final.artifact_hashes.get("strategy_version")
            != canonical_sha256(release.to_dict())
            or final.artifact_hashes.get("research_evidence")
            != canonical_sha256(matching_evidence[0].to_dict())
        ):
            raise EvidenceRequiredError(
                f"frozen credential and strategy version differ: {credential_id}"
            )
        return "SGC_VALIDATED"

    def validate_all(self) -> dict[str, int]:
        strategies = self.list_families()
        version_count = 0
        evidence_count = 0
        event_count = 0
        credential_count = 0
        credential_ids: set[str] = set()
        names: set[str] = set()
        for strategy in strategies:
            normalized = _normalized_name(strategy.name)
            if normalized in names:
                raise RegistryError(f"duplicate active strategy name: {strategy.name}")
            names.add(normalized)
            versions = self._versions(strategy.strategy_id)
            version_count += len(versions)
            events = self.lifecycle_events(strategy.strategy_id)
            evidence = self.evidence(strategy.strategy_id)
            event_count += len(events)
            evidence_count += len(evidence)
            credential_dir = self._strategy_dir(strategy.strategy_id) / "credentials"
            for path in credential_dir.glob("*.jsonl") if credential_dir.exists() else ():
                credential = self.get_governance_credential(strategy.strategy_id, path.stem)
                if credential.credential_id in credential_ids:
                    raise RegistryError(
                        f"duplicate governance credential id: {credential.credential_id}"
                    )
                credential_ids.add(credential.credential_id)
                credential_count += 1
                if credential.stage is GovernanceStage.VERSION_FROZEN:
                    final = credential.seals[-1]
                    release_id = final.content.get("release_id")
                    if not isinstance(release_id, str) or "-" not in release_id:
                        raise RegistryError(
                            f"frozen credential has no release identity: {credential.credential_id}"
                        )
                    matching = [item for item in versions if item.release_id == release_id]
                    if len(matching) != 1:
                        raise RegistryError(
                            f"frozen credential release is missing: {credential.credential_id}"
                        )
                    version = matching[0]
                    submission = next(
                        seal
                        for seal in reversed(credential.seals)
                        if seal.stage is GovernanceStage.CANDIDATE_SUBMITTED
                    )
                    adjudication = next(
                        seal
                        for seal in reversed(credential.seals)
                        if seal.stage is GovernanceStage.TDR_ADJUDICATED
                    )
                    approval = next(
                        seal
                        for seal in reversed(credential.seals)
                        if seal.stage is GovernanceStage.FREEZE_APPROVED
                    )
                    snapshot = CandidateSnapshot.from_dict(
                        dict(submission.content["candidate_snapshot"])
                    )
                    mandate = EvaluationMandate.from_dict(
                        dict(submission.content["evaluation_mandate"])
                    )
                    report = AdjudicationReport.from_dict(
                        dict(adjudication.content["adjudication_report"])
                    )
                    expected_governance = {
                        "credential_id": credential.credential_id,
                        "candidate_submission_seal_hash": submission.seal_hash,
                        "adjudication_seal_hash": adjudication.seal_hash,
                        "approval_seal_hash": approval.seal_hash,
                        "candidate_snapshot_hash": canonical_sha256(snapshot.to_dict()),
                        "evaluation_mandate_hash": mandate.mandate_hash,
                        "adjudication_report_hash": report.report_hash,
                    }
                    evidence_id = final.content.get("evidence_id")
                    matching_evidence = [
                        item for item in evidence if item.evidence_id == evidence_id
                    ]
                    if (
                        final.content.get("release_hash") != version.release_hash
                        or final.content.get("version_record_hash")
                        != canonical_sha256(version.to_dict())
                        or version.governance is None
                        or version.governance.get(
                            "credential_id" if version.schema_version == 3 else "review_id"
                        )
                        != credential.credential_id
                        or (
                            version.schema_version == 3
                            and version.governance != expected_governance
                        )
                        or len(matching_evidence) != 1
                        or final.artifact_hashes.get("strategy_version")
                        != canonical_sha256(version.to_dict())
                        or final.artifact_hashes.get("research_evidence")
                        != canonical_sha256(matching_evidence[0].to_dict())
                    ):
                        raise RegistryError(
                            f"frozen credential and strategy version differ: "
                            f"{credential.credential_id}"
                        )
            for item in versions:
                self.current_qualification(item.strategy_id, item.version)
            for item in evidence:
                version = self.get_version(item.strategy_id, item.version)
                if not version.release_hash or item.release_hash != version.release_hash:
                    raise RegistryError(f"evidence release mismatch: {item.evidence_id}")
        return {
            "strategies": len(strategies),
            "versions": version_count,
            "events": event_count,
            "evidence": evidence_count,
            "credentials": credential_count,
        }
