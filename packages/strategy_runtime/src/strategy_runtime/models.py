"""Immutable value objects and runtime records for SRT."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Mapping

from dataflows import DataRequest, DataResult, DataStatus

from .errors import RuntimeContractError


_FAMILY_ID = re.compile(r"S\d{3,}")
_VERSION = re.compile(r"v\d+")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        _thaw_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
    raise RuntimeContractError(f"{field_name} must contain only finite JSON values")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeContractError(f"{field_name} must be a non-empty string")
    return value.strip()


def _mapping(value: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeContractError(f"{field_name} must be a mapping")
    return MappingProxyType(dict(value))


def _json_mapping(value: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeContractError(f"{field_name} must be a mapping")
    return _freeze_json(value, field_name)


def _unique_text(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    normalized = tuple(_text(value, field_name) for value in values)
    if len(normalized) != len(set(normalized)):
        raise RuntimeContractError(f"{field_name} must contain unique values")
    return normalized


class CutoffRule(StrEnum):
    SIGNAL_SESSION = "SIGNAL_SESSION"
    PREVIOUS_SESSION = "PREVIOUS_SESSION"
    LATEST_AVAILABLE = "LATEST_AVAILABLE"


class PublicationStatus(StrEnum):
    READY = "READY"
    WAITING_SOURCE = "WAITING_SOURCE"
    INCOMPLETE = "INCOMPLETE"
    FAILED = "FAILED"


class RuntimeRunStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DATA_NOT_READY = "DATA_NOT_READY"


@dataclass(frozen=True, slots=True)
class ImplementationRef:
    module: str
    qualname: str
    contract_version: int
    source_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "module", _text(self.module, "implementation module"))
        object.__setattr__(self, "qualname", _text(self.qualname, "implementation qualname"))
        if self.contract_version < 1:
            raise RuntimeContractError("implementation contract_version must be positive")
        if not _SHA256.fullmatch(self.source_sha256):
            raise RuntimeContractError("implementation source_sha256 must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class ParameterSet:
    values: Mapping[str, Any]

    def __post_init__(self) -> None:
        normalized = _json_mapping(self.values, "parameter values")
        canonical_sha256(normalized)
        object.__setattr__(self, "values", normalized)

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.values)


@dataclass(frozen=True, slots=True)
class InputRequirement:
    name: str
    dataset: str
    subject: str | None
    frequency: str
    lookback_sessions: int
    cutoff_rule: CutoffRule
    maximum_staleness_days: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "input name"))
        object.__setattr__(self, "dataset", _text(self.dataset, "input dataset"))
        object.__setattr__(self, "frequency", _text(self.frequency, "input frequency"))
        if self.subject is not None:
            object.__setattr__(self, "subject", _text(self.subject, "input subject"))
        if self.lookback_sessions < 0:
            raise RuntimeContractError("input lookback_sessions must be non-negative")
        if self.maximum_staleness_days < 0:
            raise RuntimeContractError("input maximum_staleness_days must be non-negative")
        if self.cutoff_rule is not CutoffRule.LATEST_AVAILABLE and self.maximum_staleness_days:
            raise RuntimeContractError(
                "maximum_staleness_days is only valid for LATEST_AVAILABLE inputs"
            )


@dataclass(frozen=True, slots=True)
class InputContract:
    requirements: tuple[InputRequirement, ...]

    def __post_init__(self) -> None:
        if not self.requirements:
            raise RuntimeContractError("input contract must contain at least one requirement")
        names = [item.name for item in self.requirements]
        if len(names) != len(set(names)):
            raise RuntimeContractError("input requirement names must be unique")


@dataclass(frozen=True, slots=True)
class DecisionContract:
    output_kind: str
    minimum_target: float
    maximum_target: float
    effective_time_rule: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_kind", _text(self.output_kind, "decision output_kind"))
        object.__setattr__(
            self,
            "effective_time_rule",
            _text(self.effective_time_rule, "decision effective_time_rule"),
        )
        if not all(math.isfinite(item) for item in (self.minimum_target, self.maximum_target)):
            raise RuntimeContractError("decision target bounds must be finite")
        if self.minimum_target > self.maximum_target:
            raise RuntimeContractError("decision minimum_target exceeds maximum_target")


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    policy_type: str
    settings: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_type", _text(self.policy_type, "execution policy_type"))
        object.__setattr__(self, "settings", _json_mapping(self.settings, "execution settings"))
        canonical_sha256(self.settings)


@dataclass(frozen=True, slots=True)
class MonitoringPolicy:
    policy_type: str
    rules: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_type", _text(self.policy_type, "monitoring policy_type"))
        object.__setattr__(self, "rules", _json_mapping(self.rules, "monitoring rules"))
        canonical_sha256(self.rules)


@dataclass(frozen=True, slots=True)
class RequiredCapabilities:
    datasets: tuple[str, ...]
    order_types: tuple[str, ...]
    checkpoints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "datasets", _unique_text(self.datasets, "capability datasets"))
        object.__setattr__(
            self, "order_types", _unique_text(self.order_types, "capability order_types")
        )
        object.__setattr__(
            self, "checkpoints", _unique_text(self.checkpoints, "capability checkpoints")
        )


@dataclass(frozen=True, slots=True)
class ChannelCapabilities:
    order_types: tuple[str, ...]
    checkpoints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "order_types", _unique_text(self.order_types, "channel order_types")
        )
        object.__setattr__(
            self, "checkpoints", _unique_text(self.checkpoints, "channel checkpoints")
        )


@dataclass(frozen=True, slots=True)
class RuntimeDefinition:
    schema_version: int
    strategy_family_id: str
    version: str
    release_id: str
    release_hash: str
    implementation: ImplementationRef
    parameters: ParameterSet
    inputs: InputContract
    decision: DecisionContract
    execution: ExecutionPolicy
    monitoring: MonitoringPolicy
    capabilities: RequiredCapabilities

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise RuntimeContractError("runtime definition schema_version must be 1")
        if not _FAMILY_ID.fullmatch(self.strategy_family_id):
            raise RuntimeContractError("strategy_family_id must look like S001")
        if not _VERSION.fullmatch(self.version):
            raise RuntimeContractError("version must look like v1")
        if self.release_id != f"{self.strategy_family_id}-{self.version}":
            raise RuntimeContractError("release_id must equal strategy_family_id-version")
        if not _SHA256.fullmatch(self.release_hash):
            raise RuntimeContractError("release_hash must be lowercase SHA-256")
        required_datasets = {item.dataset for item in self.inputs.requirements}
        if not required_datasets.issubset(self.capabilities.datasets):
            raise RuntimeContractError("input datasets must be declared as required capabilities")

    @property
    def runtime_sha256(self) -> str:
        return canonical_sha256(
            {
                "release_id": self.release_id,
                "release_hash": self.release_hash,
                "implementation": {
                    "module": self.implementation.module,
                    "qualname": self.implementation.qualname,
                    "contract_version": self.implementation.contract_version,
                    "source_sha256": self.implementation.source_sha256,
                },
                "parameters_sha256": self.parameters.sha256,
            }
        )


@dataclass(frozen=True, slots=True, init=False)
class StrategyRelease:
    strategy_family_id: str
    version: str
    release_id: str
    release_hash: str
    payload: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "StrategyRelease":
        if not isinstance(value, Mapping):
            raise RuntimeContractError("strategy release must be a mapping")
        raw = dict(value)
        required = {"strategy_id", "version", "release_id", "release_hash", "strategy_payload"}
        missing = required - set(raw)
        if missing:
            raise RuntimeContractError(f"strategy release fields are missing: {sorted(missing)}")
        release_hash = str(raw["release_hash"])
        if not _SHA256.fullmatch(release_hash):
            raise RuntimeContractError("release_hash must be lowercase SHA-256")
        release_payload = {key: item for key, item in raw.items() if key != "release_hash"}
        if canonical_sha256(release_payload) != release_hash:
            raise RuntimeContractError("release_hash does not match the complete frozen record")
        instance = object.__new__(cls)
        object.__setattr__(instance, "strategy_family_id", str(raw["strategy_id"]))
        object.__setattr__(instance, "version", str(raw["version"]))
        object.__setattr__(instance, "release_id", str(raw["release_id"]))
        object.__setattr__(instance, "release_hash", release_hash)
        object.__setattr__(
            instance,
            "payload",
            _json_mapping(raw["strategy_payload"], "strategy payload"),
        )
        instance._validate_identity()
        return instance

    def _validate_identity(self) -> None:
        if not _FAMILY_ID.fullmatch(self.strategy_family_id):
            raise RuntimeContractError("strategy_family_id must look like S001")
        if not _VERSION.fullmatch(self.version):
            raise RuntimeContractError("version must look like v1")
        if self.release_id != f"{self.strategy_family_id}-{self.version}":
            raise RuntimeContractError("release_id must equal strategy_family_id-version")


@dataclass(frozen=True, slots=True)
class DeploymentSpec:
    deployment_id: str
    release_id: str
    release_hash: str
    symbol: str
    account_id: str
    channel_id: str
    settings: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("deployment_id", "release_id", "symbol", "account_id", "channel_id"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        if not _SHA256.fullmatch(self.release_hash):
            raise RuntimeContractError("deployment release_hash must be lowercase SHA-256")
        object.__setattr__(self, "settings", _json_mapping(self.settings, "deployment settings"))


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    account_id: str
    available_cash: float
    total_assets: float
    position_quantity: int
    revision: int
    as_of: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _text(self.account_id, "account_id"))
        if not all(
            math.isfinite(item) and item >= 0 for item in (self.available_cash, self.total_assets)
        ):
            raise RuntimeContractError("account money values must be finite and non-negative")
        if self.position_quantity < 0 or self.revision < 0:
            raise RuntimeContractError("account position and revision must be non-negative")
        if self.as_of.tzinfo is None:
            raise RuntimeContractError("account as_of must be timezone-aware")


@dataclass(frozen=True, slots=True)
class StrategyStateSnapshot:
    deployment_id: str
    release_hash: str
    revision: int
    as_of: datetime
    values: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "deployment_id", _text(self.deployment_id, "deployment_id"))
        if not _SHA256.fullmatch(self.release_hash):
            raise RuntimeContractError("state release_hash must be lowercase SHA-256")
        if self.revision < 0:
            raise RuntimeContractError("state revision must be non-negative")
        if self.as_of.tzinfo is None:
            raise RuntimeContractError("state as_of must be timezone-aware")
        object.__setattr__(self, "values", _json_mapping(self.values, "state values"))


@dataclass(frozen=True, slots=True)
class PublishedStrategyData:
    release_id: str
    release_hash: str
    status: PublicationStatus
    requested_cutoff: str
    input_requests: Mapping[str, DataRequest]
    input_results: Mapping[str, DataResult]
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "release_id", _text(self.release_id, "publication release_id"))
        object.__setattr__(
            self,
            "requested_cutoff",
            _text(self.requested_cutoff, "publication requested_cutoff"),
        )
        if not _SHA256.fullmatch(self.release_hash):
            raise RuntimeContractError("publication release_hash must be lowercase SHA-256")
        pd_requests = _mapping(self.input_requests, "publication input_requests")
        pd_results = _mapping(self.input_results, "publication input_results")
        object.__setattr__(self, "input_requests", pd_requests)
        object.__setattr__(self, "input_results", pd_results)
        if set(pd_requests) != set(pd_results):
            raise RuntimeContractError("publication request and result names must match")
        ready = self.status is PublicationStatus.READY
        if ready and (
            not pd_results
            or any(item.status is not DataStatus.READY for item in pd_results.values())
        ):
            raise RuntimeContractError("READY publication requires every input to be READY")
        if ready and self.error is not None:
            raise RuntimeContractError("READY publication cannot contain an error")
        if not ready and not self.error:
            raise RuntimeContractError("non-READY publication requires an error")

    @property
    def ready(self) -> bool:
        return self.status is PublicationStatus.READY


@dataclass(frozen=True, slots=True)
class CalculationRequest:
    deployment: DeploymentSpec
    publication: PublishedStrategyData
    account: AccountSnapshot
    state: StrategyStateSnapshot
    calculation_time: datetime

    def __post_init__(self) -> None:
        if self.calculation_time.tzinfo is None:
            raise RuntimeContractError("calculation_time must be timezone-aware")
        identities = {
            self.deployment.release_hash,
            self.publication.release_hash,
            self.state.release_hash,
        }
        if len(identities) != 1:
            raise RuntimeContractError("deployment, publication, and state release hashes differ")
        if self.deployment.release_id != self.publication.release_id:
            raise RuntimeContractError("deployment and publication release IDs differ")
        if self.deployment.account_id != self.account.account_id:
            raise RuntimeContractError("deployment and account IDs differ")
        if self.deployment.deployment_id != self.state.deployment_id:
            raise RuntimeContractError("deployment and state deployment IDs differ")
        if not self.publication.ready:
            raise RuntimeContractError("calculation requires READY published data")


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    decision_id: str
    deployment_id: str
    release_id: str
    release_hash: str
    runtime_sha256: str
    generated_at: datetime
    valid_at: datetime
    target_position: float
    account_revision: int
    state_revision: int
    input_identity_hashes: Mapping[str, str]
    evidence: Mapping[str, Any]
    next_state: Mapping[str, Any]

    def __post_init__(self) -> None:
        for field_name in ("decision_id", "deployment_id", "release_id"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        if not _SHA256.fullmatch(self.release_hash):
            raise RuntimeContractError("decision release_hash must be lowercase SHA-256")
        if not _SHA256.fullmatch(self.runtime_sha256):
            raise RuntimeContractError("decision runtime_sha256 must be lowercase SHA-256")
        if self.generated_at.tzinfo is None or self.valid_at.tzinfo is None:
            raise RuntimeContractError("decision timestamps must be timezone-aware")
        if not math.isfinite(self.target_position):
            raise RuntimeContractError("decision target_position must be finite")
        if self.account_revision < 0 or self.state_revision < 0:
            raise RuntimeContractError("decision revisions must be non-negative")
        object.__setattr__(
            self,
            "input_identity_hashes",
            _mapping(self.input_identity_hashes, "input_identity_hashes"),
        )
        for field_name in ("evidence", "next_state"):
            object.__setattr__(
                self,
                field_name,
                _json_mapping(getattr(self, field_name), field_name),
            )
        if not self.input_identity_hashes:
            raise RuntimeContractError("decision must reference input identities")
        if any(not _SHA256.fullmatch(value) for value in self.input_identity_hashes.values()):
            raise RuntimeContractError("decision input identities must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class StrategyExplanation:
    summary: str
    drivers: tuple[str, ...]
    risks: tuple[str, ...]
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "summary", _text(self.summary, "explanation summary"))
        object.__setattr__(self, "drivers", _unique_text(self.drivers, "explanation drivers"))
        object.__setattr__(self, "risks", _unique_text(self.risks, "explanation risks"))
        object.__setattr__(self, "details", _json_mapping(self.details, "explanation details"))


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    idempotency_key: str
    accepted: bool
    external_reference: str | None
    status: str
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "idempotency_key", _text(self.idempotency_key, "idempotency_key"))
        object.__setattr__(self, "status", _text(self.status, "execution status"))
        object.__setattr__(self, "message", _text(self.message, "execution message"))


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    deployment: DeploymentSpec
    account: AccountSnapshot
    decision: StrategyDecision
    policy: ExecutionPolicy

    def __post_init__(self) -> None:
        if self.deployment.account_id != self.account.account_id:
            raise RuntimeContractError("execution deployment and account IDs differ")
        if self.deployment.deployment_id != self.decision.deployment_id:
            raise RuntimeContractError("execution deployment and decision IDs differ")
        if self.deployment.release_id != self.decision.release_id:
            raise RuntimeContractError("execution deployment and decision release IDs differ")
        if self.deployment.release_hash != self.decision.release_hash:
            raise RuntimeContractError("execution deployment and decision release hashes differ")
        if self.account.revision != self.decision.account_revision:
            raise RuntimeContractError("execution account revision differs from decision")


@dataclass(frozen=True, slots=True)
class RuntimeRunResult:
    status: RuntimeRunStatus
    publication: PublishedStrategyData
    decision: StrategyDecision | None = None
    receipt: ExecutionReceipt | None = None

    def __post_init__(self) -> None:
        if self.status is RuntimeRunStatus.DATA_NOT_READY:
            if self.publication.ready or self.decision is not None or self.receipt is not None:
                raise RuntimeContractError("DATA_NOT_READY result cannot contain execution data")
            return
        if not self.publication.ready or self.decision is None or self.receipt is None:
            raise RuntimeContractError(
                "accepted or rejected runtime result requires publication and execution"
            )
        if self.status is RuntimeRunStatus.ACCEPTED and not self.receipt.accepted:
            raise RuntimeContractError("ACCEPTED result requires an accepted receipt")
        if self.status is RuntimeRunStatus.REJECTED and self.receipt.accepted:
            raise RuntimeContractError("REJECTED result requires a rejected receipt")
