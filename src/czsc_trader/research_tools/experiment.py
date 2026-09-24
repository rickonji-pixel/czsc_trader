"""TDR adapters for executing REX contracts through public platform APIs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any

from dataflows import DataRequest, DataResult, Dataflows
import pandas as pd
from research_experiment import (
    ExperimentCapability,
    ExperimentContext,
    ExperimentDefinition,
    ExperimentResources,
    ExperimentResult,
    ExperimentTrace,
    ExperimentWorkspace,
    LoadedExperiment,
    experiment_source_sha256,
)
from strategy_runtime import StrategyInit, StrategyRuntime

from ..temp_workspace import create_temporary_directory
from .evaluation import EvaluationRequest, EvaluationResult, evaluate_strategy


def _freeze_trace_json(value: Any, field_name: str) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                _trace_key(key, field_name): _freeze_trace_json(item, field_name)
                for key, item in value.items()
            }
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_trace_json(item, field_name) for item in value)
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"{field_name} must contain only finite JSON values")


def _trace_key(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} keys must be non-empty strings")
    return value.strip()


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
        self.data_requests.append(_freeze_trace_json(request, "data request trace"))

    def snapshot(self) -> ExperimentTrace:
        return ExperimentTrace(
            capabilities=tuple(self.capabilities),
            operations=tuple(self.operations),
            data_requests=tuple(self.data_requests),
        )


class _ExperimentDataAccess:
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


class _ExperimentRuntimeAccess:
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


class _ExperimentEvaluationAccess:
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


class _PlatformExperimentContext:
    """Concrete TDR implementation of the REX context protocol."""

    def __init__(
        self,
        definition: ExperimentDefinition,
        *,
        data: _ExperimentDataAccess,
        runtime: _ExperimentRuntimeAccess,
        evaluation: _ExperimentEvaluationAccess,
        workspace: ExperimentWorkspace,
        resources: ExperimentResources,
        recorder: _TraceRecorder,
    ) -> None:
        if resources.random_seed != definition.random_seed:
            raise ValueError("resource random_seed must match the experiment definition")
        self.definition = definition
        self.data = data
        self.runtime = runtime
        self.evaluation = evaluation
        self.workspace = workspace
        self.resources = resources
        self._recorder = recorder

    @property
    def trace(self) -> ExperimentTrace:
        return self._recorder.snapshot()

    def require_capability(self, capability: ExperimentCapability) -> None:
        """Declare an imminent third-party action and fail before unauthorized work."""

        _require_capability(self.definition, self._recorder, capability)


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
    """Wire public platform adapters into one capability-tracked REX context."""

    recorder = _TraceRecorder()
    if workspace is None:
        root = create_temporary_directory(
            repository_root,
            "research-experiments",
            prefix=f"{definition.experiment_id.lower()}-",
            repository_root=repository_root,
        )
        workspace = ExperimentWorkspace(root, repository_root)
    return _PlatformExperimentContext(
        definition,
        data=_ExperimentDataAccess(
            definition,
            dataflows,
            recorder,
            real_returns=real_returns,
            sealed_validation=sealed_validation,
        ),
        runtime=_ExperimentRuntimeAccess(runtime or StrategyRuntime(), recorder),
        evaluation=_ExperimentEvaluationAccess(
            definition,
            evaluator,
            recorder,
            real_returns=real_returns,
            sealed_validation=sealed_validation,
        ),
        workspace=workspace,
        resources=resources,
        recorder=recorder,
    )


def execute_experiment(
    experiment: LoadedExperiment, context: ExperimentContext
) -> ExperimentResult:
    """Execute one source-bound REX experiment and validate its result boundary."""

    if not isinstance(experiment, LoadedExperiment):
        raise TypeError("experiment must be loaded by load_experiment")
    actual_hash = experiment_source_sha256(
        experiment.root, experiment.binding.source_files
    )
    if actual_hash != experiment.binding.source_sha256:
        raise ValueError("experiment source SHA-256 differs from binding")
    if experiment.implementation.definition.sha256 != experiment.definition.sha256:
        raise ValueError("experiment definition changed after loading")
    if experiment.definition.sha256 != context.definition.sha256:
        raise ValueError("experiment and context definitions differ")
    result = experiment.implementation.execute(context)
    if not isinstance(result, ExperimentResult):
        raise TypeError("experiment returned an invalid result")
    if result.candidate is not None:
        context.require_capability(ExperimentCapability.CREATE_CANDIDATE)
    for artifact in result.artifacts:
        context.workspace.validate_artifact(artifact)
    return result


__all__ = ["create_experiment_context", "execute_experiment"]
