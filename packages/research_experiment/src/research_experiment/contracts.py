"""Stable domain contracts for executable research experiments."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from hashlib import sha256
import json
import math
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Any, Protocol

from strategy_runtime import StrategyCandidate


_EXPERIMENT_ID = re.compile(r"\d{8}_(S\d{3})_EX\d{2,}")
_STRATEGY_ID = re.compile(r"S\d{3}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DEPENDENCY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_EXACT_VERSION = re.compile(r"[0-9][A-Za-z0-9.+!-]*")


def _text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _unique_text(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    normalized = tuple(_text(value, field_name) for value in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field_name} must contain unique values")
    return normalized


def _freeze_json(value: Any, field_name: str) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                _text(key, f"{field_name} key"): _freeze_json(item, field_name)
                for key, item in value.items()
            }
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item, field_name) for item in value)
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"{field_name} must contain only finite JSON values")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        _thaw_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _safe_relative_path(value: str | Path) -> PurePosixPath:
    text = str(value)
    if not text or "\\" in text or ":" in text:
        raise ValueError("artifact path must be a relative POSIX path")
    path = PurePosixPath(text)
    if (
        not path.parts
        or path == PurePosixPath(".")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("artifact path must stay below the experiment workspace")
    return path


class ExperimentMode(StrEnum):
    """Whether the experiment explores mechanisms or produces formal evidence."""

    DISCOVERY = "DISCOVERY"
    FORMAL = "FORMAL"


class ExperimentOutcome(StrEnum):
    """Research conclusion returned by an experiment."""

    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class ExperimentCapability(StrEnum):
    """Sensitive research actions that must be declared before execution."""

    READ_REAL_RETURNS = "reads_real_returns"
    SEARCH_PARAMETERS = "searches_parameters"
    SELECT_PARAMETERS = "selects_parameters"
    CREATE_CANDIDATE = "creates_candidate"
    READ_SEALED_VALIDATION = "reads_sealed_validation"


@dataclass(frozen=True, slots=True)
class ExperimentDependency:
    """One exact third-party or platform dependency used by the experiment."""

    name: str
    version: str

    def __post_init__(self) -> None:
        name = _text(self.name, "dependency name")
        version = _text(self.version, "dependency version")
        if not _DEPENDENCY_NAME.fullmatch(name):
            raise ValueError("dependency name is invalid")
        if not _EXACT_VERSION.fullmatch(version):
            raise ValueError("dependency version must be exact")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "version", version)


@dataclass(frozen=True, slots=True)
class ExperimentCapabilities:
    """Declared sensitive behavior of an experiment."""

    reads_real_returns: bool = False
    searches_parameters: bool = False
    selects_parameters: bool = False
    creates_candidate: bool = False
    reads_sealed_validation: bool = False

    def __post_init__(self) -> None:
        for item in ExperimentCapability:
            if not isinstance(getattr(self, item.value), bool):
                raise ValueError(f"capability {item.value} must be boolean")

    def allows(self, capability: ExperimentCapability) -> bool:
        return bool(getattr(self, ExperimentCapability(capability).value))


@dataclass(frozen=True, slots=True)
class ExperimentDefinition:
    """Pre-execution research intent, boundary and reproducibility contract."""

    schema_version: int
    experiment_id: str
    strategy_id: str
    mode: ExperimentMode
    research_question: str
    hypothesis: str
    falsification_conditions: tuple[str, ...]
    development_cutoff: date
    random_seed: int
    allowed_datasets: tuple[str, ...]
    dependencies: tuple[ExperimentDependency, ...] = ()
    capabilities: ExperimentCapabilities = ExperimentCapabilities()

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("experiment schema_version must be 1")
        experiment_id = _text(self.experiment_id, "experiment_id")
        strategy_id = _text(self.strategy_id, "strategy_id")
        match = _EXPERIMENT_ID.fullmatch(experiment_id)
        if match is None:
            raise ValueError("experiment_id must match YYYYMMDD_SNNN_EXNN")
        if not _STRATEGY_ID.fullmatch(strategy_id) or match.group(1) != strategy_id:
            raise ValueError("strategy_id must match the experiment_id strategy")
        if not isinstance(self.mode, ExperimentMode):
            raise ValueError("mode must be an ExperimentMode")
        if not isinstance(self.development_cutoff, date):
            raise ValueError("development_cutoff must be a date")
        if isinstance(self.random_seed, bool) or not isinstance(self.random_seed, int):
            raise ValueError("random_seed must be an integer")
        if self.random_seed < 0:
            raise ValueError("random_seed must be non-negative")
        falsification = _unique_text(
            self.falsification_conditions, "falsification_conditions"
        )
        if not falsification:
            raise ValueError("falsification_conditions must not be empty")
        datasets = _unique_text(self.allowed_datasets, "allowed_datasets")
        if not datasets:
            raise ValueError("allowed_datasets must not be empty")
        dependencies = tuple(self.dependencies)
        if not all(isinstance(item, ExperimentDependency) for item in dependencies):
            raise ValueError("dependencies must contain ExperimentDependency values")
        dependency_names = tuple(item.name for item in dependencies)
        if len(dependency_names) != len(set(dependency_names)):
            raise ValueError("dependency names must be unique")
        if not isinstance(self.capabilities, ExperimentCapabilities):
            raise ValueError("capabilities must be ExperimentCapabilities")
        object.__setattr__(self, "experiment_id", experiment_id)
        object.__setattr__(self, "strategy_id", strategy_id)
        object.__setattr__(
            self, "research_question", _text(self.research_question, "research_question")
        )
        object.__setattr__(self, "hypothesis", _text(self.hypothesis, "hypothesis"))
        object.__setattr__(self, "falsification_conditions", falsification)
        object.__setattr__(self, "allowed_datasets", datasets)
        object.__setattr__(self, "dependencies", dependencies)

    @property
    def sha256(self) -> str:
        return _canonical_sha256(
            {
                "schema_version": self.schema_version,
                "experiment_id": self.experiment_id,
                "strategy_id": self.strategy_id,
                "mode": self.mode.value,
                "research_question": self.research_question,
                "hypothesis": self.hypothesis,
                "falsification_conditions": self.falsification_conditions,
                "development_cutoff": self.development_cutoff.isoformat(),
                "random_seed": self.random_seed,
                "allowed_datasets": self.allowed_datasets,
                "dependencies": tuple(
                    {"name": item.name, "version": item.version}
                    for item in self.dependencies
                ),
                "capabilities": {
                    item.value: self.capabilities.allows(item)
                    for item in ExperimentCapability
                },
            }
        )


@dataclass(frozen=True, slots=True)
class ExperimentResources:
    """Explicit local resource budget supplied by the platform."""

    max_workers: int
    random_seed: int
    native_threads_per_worker: int = 1
    max_evaluations: int | None = None

    def __post_init__(self) -> None:
        for name in ("max_workers", "native_threads_per_worker"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if isinstance(self.random_seed, bool) or not isinstance(self.random_seed, int):
            raise ValueError("random_seed must be an integer")
        if self.random_seed < 0:
            raise ValueError("random_seed must be non-negative")
        if self.max_evaluations is not None and (
            isinstance(self.max_evaluations, bool)
            or not isinstance(self.max_evaluations, int)
            or self.max_evaluations < 1
        ):
            raise ValueError("max_evaluations must be a positive integer or None")


@dataclass(frozen=True, slots=True)
class ExperimentArtifact:
    """One immutable file produced below the governed experiment workspace."""

    path: str
    kind: str
    sha256: str

    def __post_init__(self) -> None:
        path = _safe_relative_path(self.path)
        kind = _text(self.kind, "artifact kind")
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("artifact sha256 must be lowercase SHA-256")
        object.__setattr__(self, "path", path.as_posix())
        object.__setattr__(self, "kind", kind)


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    """Machine-checkable research facts returned by one experiment execution."""

    outcome: ExperimentOutcome
    facts: Mapping[str, Any]
    diagnostics: Mapping[str, Any]
    artifacts: tuple[ExperimentArtifact, ...] = ()
    candidate: StrategyCandidate | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ExperimentOutcome):
            raise ValueError("outcome must be an ExperimentOutcome")
        if not isinstance(self.facts, Mapping):
            raise ValueError("facts must be a mapping")
        if not isinstance(self.diagnostics, Mapping):
            raise ValueError("diagnostics must be a mapping")
        facts = _freeze_json(self.facts, "facts")
        diagnostics = _freeze_json(self.diagnostics, "diagnostics")
        artifacts = tuple(self.artifacts)
        if not all(isinstance(item, ExperimentArtifact) for item in artifacts):
            raise ValueError("artifacts must contain ExperimentArtifact values")
        paths = tuple(item.path for item in artifacts)
        if len(paths) != len(set(paths)):
            raise ValueError("artifact paths must be unique")
        if self.candidate is not None and not isinstance(self.candidate, StrategyCandidate):
            raise ValueError("candidate must be a StrategyCandidate or None")
        object.__setattr__(self, "facts", facts)
        object.__setattr__(self, "diagnostics", diagnostics)
        object.__setattr__(self, "artifacts", artifacts)


@dataclass(frozen=True, slots=True)
class ExperimentTrace:
    """Read-only execution trace captured at platform boundaries."""

    capabilities: tuple[ExperimentCapability, ...]
    operations: tuple[str, ...]
    data_requests: tuple[Mapping[str, Any], ...]


class ExperimentWorkspace:
    """Repository-local scratch space and artifact identity boundary."""

    def __init__(self, root: Path, repository_root: Path) -> None:
        root = Path(root).resolve()
        repository_root = Path(repository_root).resolve()
        governed_root = (repository_root / ".tmp").resolve()
        try:
            root.relative_to(governed_root)
        except ValueError as exc:
            raise ValueError("experiment workspace must be below repository .tmp") from exc
        root.mkdir(parents=True, exist_ok=True)
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def path(self, relative_path: str | Path) -> Path:
        relative = _safe_relative_path(relative_path)
        target = self._root.joinpath(*relative.parts).resolve()
        try:
            target.relative_to(self._root)
        except ValueError as exc:
            raise ValueError("workspace path escapes the experiment workspace") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def register_artifact(self, relative_path: str | Path, kind: str) -> ExperimentArtifact:
        target = self.path(relative_path)
        if not target.is_file():
            raise FileNotFoundError(f"experiment artifact does not exist: {target}")
        return ExperimentArtifact(
            path=_safe_relative_path(relative_path).as_posix(),
            kind=kind,
            sha256=sha256(target.read_bytes()).hexdigest(),
        )

    def validate_artifact(self, artifact: ExperimentArtifact) -> None:
        target = self.path(artifact.path)
        if not target.is_file():
            raise FileNotFoundError(f"experiment artifact does not exist: {target}")
        if sha256(target.read_bytes()).hexdigest() != artifact.sha256:
            raise ValueError(f"experiment artifact hash differs: {artifact.path}")


class ExperimentContext(Protocol):
    """Research-facing protocol implemented by a platform context."""

    definition: ExperimentDefinition
    data: Any
    runtime: Any
    evaluation: Any
    workspace: ExperimentWorkspace
    resources: ExperimentResources

    @property
    def trace(self) -> ExperimentTrace: ...

    def require_capability(self, capability: ExperimentCapability) -> None: ...


class ResearchExperiment(ABC):
    """Researcher's executable anchor for one falsifiable experiment."""

    @property
    @abstractmethod
    def definition(self) -> ExperimentDefinition:
        """Return the immutable pre-execution definition."""

    @abstractmethod
    def execute(self, context: ExperimentContext) -> ExperimentResult:
        """Execute only through the supplied, capability-tracked context."""


__all__ = [
    "ExperimentArtifact",
    "ExperimentCapabilities",
    "ExperimentCapability",
    "ExperimentContext",
    "ExperimentDefinition",
    "ExperimentDependency",
    "ExperimentMode",
    "ExperimentOutcome",
    "ExperimentResources",
    "ExperimentResult",
    "ExperimentTrace",
    "ExperimentWorkspace",
    "ResearchExperiment",
]
