"""Stable contracts for executable, auditable research experiments."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from hashlib import sha256
import importlib
import importlib.machinery
from importlib.metadata import PackageNotFoundError, version as distribution_version
import json
import math
from pathlib import Path, PurePosixPath
import re
import sys
from threading import RLock
from types import MappingProxyType, ModuleType
from typing import Any

import pandas as pd
from dataflows import DataRequest, DataResult, Dataflows
from strategy_runtime import (
    StrategyCandidate,
    StrategyInit,
    StrategyRuntime,
)

from ..temp_workspace import create_temporary_directory
from .evaluation import EvaluationRequest, EvaluationResult, evaluate_strategy


_EXPERIMENT_ID = re.compile(r"\d{8}_(S\d{3})_EX\d{2,}")
_STRATEGY_ID = re.compile(r"S\d{3}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DEPENDENCY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_EXACT_VERSION = re.compile(r"[0-9][A-Za-z0-9.+!-]*")
_LOAD_LOCK = RLock()


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
        object.__setattr__(self, "research_question", _text(self.research_question, "research_question"))
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


class ResearchExperiment(ABC):
    """Researcher's executable anchor for one falsifiable experiment."""

    @property
    @abstractmethod
    def definition(self) -> ExperimentDefinition:
        """Return the immutable pre-execution definition."""

    @abstractmethod
    def execute(self, context: ExperimentContext) -> ExperimentResult:
        """Execute only through the supplied, capability-tracked context."""


@dataclass(frozen=True, slots=True)
class ExperimentTrace:
    """Read-only execution trace captured at platform boundaries."""

    capabilities: tuple[ExperimentCapability, ...]
    operations: tuple[str, ...]
    data_requests: tuple[Mapping[str, Any], ...]


class _TraceRecorder:
    def __init__(self) -> None:
        self.capabilities: list[ExperimentCapability] = []
        self.operations: list[str] = []
        self.data_requests: list[Mapping[str, Any]] = []

    def record_capability(self, capability: ExperimentCapability) -> None:
        if capability not in self.capabilities:
            self.capabilities.append(capability)

    def record_operation(self, operation: str) -> None:
        self.operations.append(operation)

    def record_data_request(self, request: Mapping[str, Any]) -> None:
        self.data_requests.append(_freeze_json(request, "data request trace"))

    def snapshot(self) -> ExperimentTrace:
        return ExperimentTrace(
            capabilities=tuple(self.capabilities),
            operations=tuple(self.operations),
            data_requests=tuple(self.data_requests),
        )


class ExperimentDataAccess:
    """Capability- and cutoff-aware DFLS adapter."""

    def __init__(
        self,
        definition: ExperimentDefinition,
        dataflows: Dataflows,
        recorder: _TraceRecorder,
        *,
        real_returns: bool,
        sealed_validation: bool,
    ) -> None:
        self._definition = definition
        self._dataflows = dataflows
        self._recorder = recorder
        self._real_returns = real_returns
        self._sealed_validation = sealed_validation

    def fetch(self, request: DataRequest) -> DataResult:
        if not isinstance(request, DataRequest):
            raise TypeError("data fetch requires a DataRequest")
        if request.dataset not in self._definition.allowed_datasets:
            raise PermissionError(f"dataset was not declared: {request.dataset}")
        cutoff = pd.Timestamp(self._definition.development_cutoff)
        if pd.Timestamp(request.end).normalize() > cutoff:
            raise PermissionError("data request exceeds the development cutoff")
        if request.required_cutoff is not None and (
            pd.Timestamp(request.required_cutoff).normalize() > cutoff
        ):
            raise PermissionError("required cutoff exceeds the development cutoff")
        if self._real_returns:
            _require_capability(
                self._definition, self._recorder, ExperimentCapability.READ_REAL_RETURNS
            )
        if self._sealed_validation:
            _require_capability(
                self._definition,
                self._recorder,
                ExperimentCapability.READ_SEALED_VALIDATION,
            )
        result = self._dataflows.fetch(request)
        identity = None
        if result.identity is not None:
            identity = {
                "dataset": result.identity.dataset,
                "source": result.identity.source,
                "symbol": result.identity.symbol,
                "data_start": result.identity.data_start,
                "data_cutoff": result.identity.data_cutoff,
                "content_sha256": result.identity.content_sha256,
            }
        self._recorder.record_operation("data.fetch")
        self._recorder.record_data_request(
            {
                "dataset": request.dataset,
                "symbol": request.symbol,
                "start": request.start,
                "end": request.end,
                "required_cutoff": request.required_cutoff,
                "frequency": request.frequency,
                "status": result.status.value,
                "identity": identity,
            }
        )
        return result


class ExperimentRuntimeAccess:
    """Tracked adapter over the public SRT runtime surface."""

    def __init__(self, runtime: StrategyRuntime, recorder: _TraceRecorder) -> None:
        self._runtime = runtime
        self._recorder = recorder

    def describe(
        self,
        source: Any,
        *,
        symbol: str | None = None,
        source_root: Path | None = None,
        runtime_binding: Mapping[str, object] | None = None,
    ) -> Any:
        result = self._runtime.describe(
            source,
            symbol=symbol,
            source_root=source_root,
            runtime_binding=runtime_binding,
        )
        self._recorder.record_operation("runtime.describe")
        return result

    def create(self, request: StrategyInit) -> Any:
        if not isinstance(request, StrategyInit):
            raise TypeError("runtime create requires a StrategyInit")
        result = self._runtime.create(request)
        self._recorder.record_operation("runtime.create")
        return result


class ExperimentEvaluationAccess:
    """Tracked adapter over the researcher-facing evaluation Harness."""

    def __init__(
        self,
        definition: ExperimentDefinition,
        evaluator: Callable[[EvaluationRequest], EvaluationResult],
        recorder: _TraceRecorder,
        *,
        real_returns: bool,
        sealed_validation: bool,
    ) -> None:
        self._definition = definition
        self._evaluator = evaluator
        self._recorder = recorder
        self._real_returns = real_returns
        self._sealed_validation = sealed_validation

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        if not isinstance(request, EvaluationRequest):
            raise TypeError("evaluation requires an EvaluationRequest")
        if request.experiment_id != self._definition.experiment_id:
            raise ValueError("evaluation request belongs to another experiment")
        if request.development_cutoff != self._definition.development_cutoff:
            raise ValueError("evaluation request development cutoff differs")
        if self._real_returns:
            _require_capability(
                self._definition, self._recorder, ExperimentCapability.READ_REAL_RETURNS
            )
        if self._sealed_validation:
            _require_capability(
                self._definition,
                self._recorder,
                ExperimentCapability.READ_SEALED_VALIDATION,
            )
        result = self._evaluator(request)
        if not isinstance(result, EvaluationResult):
            raise TypeError("evaluation Harness returned an invalid result")
        self._recorder.record_operation("evaluation.evaluate")
        return result


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
        self._repository_root = repository_root

    @classmethod
    def create(cls, repository_root: Path, experiment_id: str) -> ExperimentWorkspace:
        experiment_id = _text(experiment_id, "experiment_id")
        root = create_temporary_directory(
            repository_root,
            "research-experiments",
            prefix=f"{experiment_id.lower()}-",
            repository_root=repository_root,
        )
        return cls(root, repository_root)

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


class ExperimentContext:
    """The only platform surface supplied to an executable research experiment."""

    def __init__(
        self,
        definition: ExperimentDefinition,
        *,
        data: ExperimentDataAccess,
        runtime: ExperimentRuntimeAccess,
        evaluation: ExperimentEvaluationAccess,
        workspace: ExperimentWorkspace,
        resources: ExperimentResources,
        recorder: _TraceRecorder,
    ) -> None:
        if resources.random_seed != definition.random_seed:
            raise ValueError("resource random_seed must match the experiment definition")
        self._definition = definition
        self.data = data
        self.runtime = runtime
        self.evaluation = evaluation
        self.workspace = workspace
        self.resources = resources
        self._recorder = recorder

    @property
    def definition(self) -> ExperimentDefinition:
        return self._definition

    @property
    def trace(self) -> ExperimentTrace:
        return self._recorder.snapshot()

    def require_capability(self, capability: ExperimentCapability) -> None:
        """Declare an imminent third-party action and fail before unauthorized work."""

        _require_capability(self._definition, self._recorder, capability)


def _require_capability(
    definition: ExperimentDefinition,
    recorder: _TraceRecorder,
    capability: ExperimentCapability,
) -> None:
    capability = ExperimentCapability(capability)
    if not definition.capabilities.allows(capability):
        raise PermissionError(f"experiment did not declare capability: {capability.value}")
    recorder.record_capability(capability)


def create_experiment_context(
    definition: ExperimentDefinition,
    *,
    repository_root: Path,
    dataflows: Dataflows,
    resources: ExperimentResources,
    runtime: StrategyRuntime | None = None,
    evaluator: Callable[[EvaluationRequest], EvaluationResult] = evaluate_strategy,
    workspace: ExperimentWorkspace | None = None,
    real_returns: bool = False,
    sealed_validation: bool = False,
) -> ExperimentContext:
    """Wire public platform adapters into one capability-tracked context."""

    recorder = _TraceRecorder()
    resolved_workspace = workspace or ExperimentWorkspace.create(
        repository_root, definition.experiment_id
    )
    return ExperimentContext(
        definition,
        data=ExperimentDataAccess(
            definition,
            dataflows,
            recorder,
            real_returns=real_returns,
            sealed_validation=sealed_validation,
        ),
        runtime=ExperimentRuntimeAccess(runtime or StrategyRuntime(), recorder),
        evaluation=ExperimentEvaluationAccess(
            definition,
            evaluator,
            recorder,
            real_returns=real_returns,
            sealed_validation=sealed_validation,
        ),
        workspace=resolved_workspace,
        resources=resources,
        recorder=recorder,
    )


def execute_experiment(
    experiment: ResearchExperiment, context: ExperimentContext
) -> ExperimentResult:
    """Execute one experiment and validate its declared result boundary."""

    if not isinstance(experiment, ResearchExperiment):
        raise TypeError("experiment must implement ResearchExperiment")
    if experiment.definition.sha256 != context.definition.sha256:
        raise ValueError("experiment and context definitions differ")
    result = experiment.execute(context)
    if not isinstance(result, ExperimentResult):
        raise TypeError("experiment returned an invalid result")
    if result.candidate is not None:
        context.require_capability(ExperimentCapability.CREATE_CANDIDATE)
    for artifact in result.artifacts:
        context.workspace.validate_artifact(artifact)
    return result


@dataclass(frozen=True, slots=True)
class ExperimentBinding:
    """Identity of an isolated experiment implementation closure."""

    schema_version: int
    module: str
    qualname: str
    source_files: tuple[str, ...]
    source_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("experiment binding schema_version must be 1")
        module = _text(self.module, "binding module")
        qualname = _text(self.qualname, "binding qualname")
        if not all(part.isidentifier() for part in module.split(".")):
            raise ValueError("binding module must be a dotted Python identifier")
        if not all(part.isidentifier() for part in qualname.split(".")):
            raise ValueError("binding qualname must be a dotted Python identifier")
        source_files = tuple(
            _safe_relative_path(item).as_posix() for item in self.source_files
        )
        if not source_files or len(source_files) != len(set(source_files)):
            raise ValueError("binding source_files must be non-empty and unique")
        expected_module = f"{module.replace('.', '/')}.py"
        package_module = f"{module.replace('.', '/')}/__init__.py"
        if expected_module not in source_files and package_module not in source_files:
            raise ValueError("binding source_files do not contain the implementation module")
        if not _SHA256.fullmatch(self.source_sha256):
            raise ValueError("binding source_sha256 must be lowercase SHA-256")
        object.__setattr__(self, "module", module)
        object.__setattr__(self, "qualname", qualname)
        object.__setattr__(self, "source_files", source_files)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ExperimentBinding:
        expected = {
            "schema_version",
            "module",
            "qualname",
            "source_files",
            "source_sha256",
        }
        if set(payload) != expected:
            raise ValueError("experiment binding fields differ from schema")
        source_files = payload["source_files"]
        if not isinstance(source_files, list):
            raise ValueError("binding source_files must be a list")
        return cls(
            schema_version=payload["schema_version"],
            module=payload["module"],
            qualname=payload["qualname"],
            source_files=tuple(source_files),
            source_sha256=payload["source_sha256"],
        )


def experiment_source_sha256(root: Path, source_files: tuple[str, ...]) -> str:
    """Return a deterministic identity for the complete declared source closure."""

    root = Path(root).resolve()
    digest = sha256()
    normalized = tuple(_safe_relative_path(item).as_posix() for item in source_files)
    if len(normalized) != len(set(normalized)):
        raise ValueError("source_files must be unique")
    for name in sorted(normalized):
        target = root.joinpath(*PurePosixPath(name).parts).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError("experiment source escapes its root") from exc
        if not target.is_file():
            raise FileNotFoundError(f"experiment source does not exist: {name}")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(target.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_experiment(root: Path) -> ResearchExperiment:
    """Load one hash-verified experiment without adding its path to ``sys.path``."""

    root = Path(root).resolve()
    binding_path = root / "experiment_binding.json"
    try:
        raw = json.loads(binding_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read experiment binding: {binding_path}") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("experiment binding must be a JSON object")
    binding = ExperimentBinding.from_mapping(raw)
    actual_hash = experiment_source_sha256(root, binding.source_files)
    if actual_hash != binding.source_sha256:
        raise ValueError("experiment source SHA-256 differs from binding")

    root_identity = sha256(str(root).encode("utf-8")).hexdigest()[:12]
    namespace = f"_czsc_research_experiment_{root_identity}_{actual_hash[:12]}"
    full_module = f"{namespace}.{binding.module}"
    created_namespace = False
    with _LOAD_LOCK:
        if namespace not in sys.modules:
            package = ModuleType(namespace)
            package.__path__ = [str(root)]
            package.__package__ = namespace
            package.__spec__ = importlib.machinery.ModuleSpec(
                namespace, loader=None, is_package=True
            )
            sys.modules[namespace] = package
            created_namespace = True
        try:
            module = importlib.import_module(full_module)
            declared = set(binding.source_files)
            loaded_sources: set[str] = set()
            for name, loaded in tuple(sys.modules.items()):
                if name != namespace and not name.startswith(f"{namespace}."):
                    continue
                file_name = getattr(loaded, "__file__", None)
                if file_name is None:
                    continue
                loaded_path = Path(file_name).resolve()
                try:
                    relative = loaded_path.relative_to(root).as_posix()
                except ValueError as exc:
                    raise ValueError("experiment imported source outside its root") from exc
                loaded_sources.add(relative)
            undeclared = loaded_sources - declared
            if undeclared:
                raise ValueError(
                    "experiment loaded undeclared source files: "
                    + ", ".join(sorted(undeclared))
                )
            implementation: Any = module
            for part in binding.qualname.split("."):
                implementation = getattr(implementation, part)
            experiment = implementation()
        except Exception:
            if created_namespace:
                for name in tuple(sys.modules):
                    if name == namespace or name.startswith(f"{namespace}."):
                        sys.modules.pop(name, None)
            raise
    if not isinstance(experiment, ResearchExperiment):
        raise TypeError("bound implementation must instantiate ResearchExperiment")
    if experiment.definition.experiment_id != root.name:
        raise ValueError("experiment definition id differs from its directory")
    if _STRATEGY_ID.fullmatch(root.parent.name) and (
        experiment.definition.strategy_id != root.parent.name
    ):
        raise ValueError("experiment definition strategy differs from its directory")
    for dependency in experiment.definition.dependencies:
        try:
            installed = distribution_version(dependency.name)
        except PackageNotFoundError as exc:
            raise ValueError(
                f"experiment dependency is not installed: {dependency.name}"
            ) from exc
        if installed != dependency.version:
            raise ValueError(
                "experiment dependency version differs: "
                f"{dependency.name} requires {dependency.version}, installed {installed}"
            )
    return experiment


__all__ = [
    "ExperimentArtifact",
    "ExperimentBinding",
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
    "create_experiment_context",
    "execute_experiment",
    "experiment_source_sha256",
    "load_experiment",
]
