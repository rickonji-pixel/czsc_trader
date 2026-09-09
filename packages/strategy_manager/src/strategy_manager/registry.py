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
    EvidencePhase,
    LifecycleEvent,
    PerformanceEvidence,
    Qualification,
    Strategy,
    StrategyVersion,
    canonical_sha256,
)
from .validation import require_string


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

    @staticmethod
    def _atomic_write(path: Path, text: str, expected: bytes | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        current = path.read_bytes() if path.exists() else None
        if current != expected:
            raise RegistryError(f"concurrent change detected: {path}")
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(text, encoding="utf-8", newline="\n")
        temporary.replace(path)

    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
        expected = path.read_bytes() if path.exists() else None
        self._atomic_write(path, _canonical_json(value), expected)

    def _append_jsonl(self, path: Path, value: dict[str, Any]) -> None:
        expected = path.read_bytes() if path.exists() else None
        existing = expected.decode("utf-8") if expected else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        line = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self._atomic_write(path, f"{existing}{line}\n", expected)

    def _strategy_dir(self, strategy_id: str) -> Path:
        return self.root / strategy_id

    def _version_path(self, strategy_id: str, version: str) -> Path:
        return self._strategy_dir(strategy_id) / "versions" / f"{version}.json"

    def list_strategies(self) -> list[Strategy]:
        return [self.get_strategy(item["strategy_id"]) for item in self._load_registry()["strategies"]]

    def get_strategy(self, strategy_id: str) -> Strategy:
        path = self._strategy_dir(strategy_id) / "strategy.json"
        if not path.exists():
            raise RegistryError(f"unknown strategy: {strategy_id}")
        return Strategy.from_dict(self._read_json(path))

    def resolve_strategy_identity(self, reference: str) -> Strategy:
        registry = self._load_registry()
        match = next(
            (
                item
                for item in registry["strategies"]
                if reference == item["strategy_id"]
                or reference in item.get("aliases", [])
            ),
            None,
        )
        if match is None:
            raise RegistryError(f"unknown strategy reference: {reference}")
        return self.get_strategy(match["strategy_id"])

    def get_version(self, strategy_id: str, version: str) -> StrategyVersion:
        path = self._version_path(strategy_id, version)
        if not path.exists():
            raise RegistryError(f"unknown strategy version: {strategy_id}-{version}")
        raw = self._read_json(path)
        release_hash = raw.get("release_hash")
        if release_hash:
            release_payload = {key: value for key, value in raw.items() if key != "release_hash"}
            if canonical_sha256(release_payload) != release_hash:
                raise ImmutableVersionError(f"release hash mismatch: {strategy_id}-{version}")
        try:
            return StrategyVersion.from_dict(raw)
        except ValidationError as exc:
            raise RegistryError(f"invalid strategy version {strategy_id}-{version}: {exc}") from exc

    def _versions(self, strategy_id: str) -> list[StrategyVersion]:
        directory = self._strategy_dir(strategy_id) / "versions"
        if not directory.exists():
            return []
        versions = [self.get_version(strategy_id, path.stem) for path in directory.glob("v*.json")]
        return sorted(versions, key=lambda item: int(item.version[1:]))

    def versions(self, strategy_id: str) -> tuple[StrategyVersion, ...]:
        """Return registered versions, including an empty tuple for a new identity."""
        self.get_strategy(strategy_id)
        return tuple(self._versions(strategy_id))

    def resolve_strategy(self, reference: str, version: str | None = None) -> StrategyVersion:
        strategy = self.resolve_strategy_identity(reference)
        versions = self._versions(strategy.strategy_id)
        if version is not None:
            return self.get_version(strategy.strategy_id, version)
        if not versions:
            raise RegistryError(f"strategy has no versions: {strategy.strategy_id}")
        return versions[-1]

    def create_strategy(
        self,
        strategy: Strategy | dict[str, Any],
        *,
        actor: str,
        reason: str,
        aliases: list[str] | None = None,
    ) -> Strategy:
        require_string(actor, "actor")
        require_string(reason, "reason")
        model = strategy if isinstance(strategy, Strategy) else Strategy.from_dict(strategy)
        registry = self._load_registry()
        if any(item["strategy_id"] == model.strategy_id for item in registry["strategies"]):
            raise RegistryError(f"strategy already exists: {model.strategy_id}")
        normalized = _normalized_name(model.name)
        if any(_normalized_name(existing.name) == normalized for existing in self.list_strategies()):
            raise RegistryError(f"active strategy name already exists: {model.name}")
        alias_list = aliases or []
        known_references = {
            reference
            for item in registry["strategies"]
            for reference in [item["strategy_id"], *item.get("aliases", [])]
        }
        if model.strategy_id in alias_list or any(alias in known_references for alias in alias_list):
            raise RegistryError("strategy aliases must be unique")
        registry["strategies"].append(
            {"strategy_id": model.strategy_id, "path": model.strategy_id, "aliases": alias_list}
        )
        registry["strategies"].sort(key=lambda item: item["strategy_id"])
        strategy_path = self._strategy_dir(model.strategy_id) / "strategy.json"
        self._atomic_write(strategy_path, _canonical_json(model.to_dict()), None)
        self._write_json(self.registry_path, registry)
        return model

    def create_version(
        self,
        version: StrategyVersion | dict[str, Any],
        *,
        actor: str,
        reason: str,
    ) -> tuple[StrategyVersion, LifecycleEvent]:
        actor = require_string(actor, "actor")
        reason = require_string(reason, "reason")
        model = version if isinstance(version, StrategyVersion) else StrategyVersion.from_dict(version)
        self.get_strategy(model.strategy_id)
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
        self._append_jsonl(self._strategy_dir(model.strategy_id) / "lifecycle.jsonl", event.to_dict())
        return model, event

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
        events = [
            event for event in self.lifecycle_events(strategy_id) if event.version == version
        ]
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
            destination = self._strategy_dir(model.strategy_id) / "evidence" / f"{model.evidence_id}.json"
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

    def freeze_version(
        self,
        strategy_id: str,
        version: str,
        *,
        actor: str,
        reason: str,
        evidence: PerformanceEvidence | dict[str, Any],
    ) -> tuple[StrategyVersion, LifecycleEvent]:
        actor = require_string(actor, "actor")
        reason = require_string(reason, "reason")
        current = self.current_qualification(strategy_id, version)
        validate_transition(current, Qualification.PAPER_READY)
        research = self.get_version(strategy_id, version)
        if research.release_hash is not None:
            raise ImmutableVersionError(f"strategy version is already frozen: {research.release_id}")
        release_hash = canonical_sha256(research.release_payload())
        frozen = replace(research, release_hash=release_hash)
        incoming = evidence.to_dict() if isinstance(evidence, PerformanceEvidence) else dict(evidence)
        incoming.update(
            {"strategy_id": strategy_id, "version": version, "release_hash": release_hash}
        )
        research_evidence = PerformanceEvidence.from_dict(incoming)
        if research_evidence.phase is not EvidencePhase.RESEARCH_BACKTEST:
            raise EvidenceRequiredError("freeze requires RESEARCH_BACKTEST evidence")
        if any(item.evidence_id == research_evidence.evidence_id for item in self.evidence(strategy_id)):
            raise RegistryError(f"evidence_id already exists: {research_evidence.evidence_id}")
        event = self._event(
            "VERSION_FROZEN",
            strategy_id,
            version,
            current,
            Qualification.PAPER_READY,
            actor,
            reason,
            [research_evidence.evidence_id],
            release_hash,
        )
        version_path = self._version_path(strategy_id, version)
        expected_version = version_path.read_bytes()
        evidence_path = self._strategy_dir(strategy_id) / "evidence.jsonl"
        lifecycle_path = self._strategy_dir(strategy_id) / "lifecycle.jsonl"
        expected_evidence = evidence_path.read_bytes() if evidence_path.exists() else None
        expected_lifecycle = lifecycle_path.read_bytes() if lifecycle_path.exists() else None
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
                f"{strategy_id}-{version} is not deployable to {environment}: "
                f"{qualification.value}"
            )
        return self.get_version(strategy_id, version)

    def validate_all(self) -> dict[str, int]:
        strategies = self.list_strategies()
        version_count = 0
        evidence_count = 0
        event_count = 0
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
        }
