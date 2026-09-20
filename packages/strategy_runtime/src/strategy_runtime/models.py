"""Immutable value objects and runtime records for SRT."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import StrEnum
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Mapping

import pandas as pd

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
class HistoryPolicy:
    """Declare how much published history participates in strategy replay."""

    mode: str = "FULL_PUBLICATION_REPLAY"
    canonical_start: str | None = None
    required_input_start: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"FULL_PUBLICATION_REPLAY", "CANONICAL_REPLAY"}:
            raise RuntimeContractError("history mode is unsupported")
        if self.mode == "CANONICAL_REPLAY":
            if not self.canonical_start:
                raise RuntimeContractError("canonical replay requires canonical_start")
            try:
                date.fromisoformat(self.canonical_start)
            except ValueError as exc:
                raise RuntimeContractError("history canonical_start must be an ISO date") from exc
        elif self.canonical_start is not None:
            raise RuntimeContractError(
                "canonical_start is only valid for CANONICAL_REPLAY"
            )
        if self.required_input_start is not None:
            try:
                input_start = date.fromisoformat(self.required_input_start)
            except ValueError as exc:
                raise RuntimeContractError(
                    "history required_input_start must be an ISO date"
                ) from exc
            if self.canonical_start and input_start > date.fromisoformat(self.canonical_start):
                raise RuntimeContractError(
                    "history required_input_start follows canonical_start"
                )

    def publication_start(self, requested_start: date) -> date:
        if self.required_input_start is not None:
            return min(requested_start, date.fromisoformat(self.required_input_start))
        if self.canonical_start is None:
            return requested_start
        return min(requested_start, date.fromisoformat(self.canonical_start))


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
    version: str | None
    release_id: str
    release_hash: str
    implementation: ImplementationRef
    parameters: ParameterSet
    inputs: InputContract
    decision: DecisionContract
    execution: ExecutionPolicy
    monitoring: MonitoringPolicy
    capabilities: RequiredCapabilities
    state_mode: str = "STATELESS"
    identity_kind: str = "RELEASE"
    candidate_id: str | None = None
    history: HistoryPolicy = field(default_factory=HistoryPolicy)

    def __post_init__(self) -> None:
        if self.schema_version not in {1, 2}:
            raise RuntimeContractError("runtime definition schema_version must be 1 or 2")
        if not _FAMILY_ID.fullmatch(self.strategy_family_id):
            raise RuntimeContractError("strategy_family_id must look like S001")
        if self.identity_kind == "RELEASE":
            if not isinstance(self.version, str) or not _VERSION.fullmatch(self.version):
                raise RuntimeContractError("version must look like v1")
            if self.candidate_id is not None:
                raise RuntimeContractError("release runtime cannot carry a candidate identity")
            if self.release_id != f"{self.strategy_family_id}-{self.version}":
                raise RuntimeContractError("release_id must equal strategy_family_id-version")
        elif self.identity_kind == "CANDIDATE":
            if self.schema_version != 2 or self.version is not None:
                raise RuntimeContractError(
                    "candidate runtime requires schema 2 and no frozen version"
                )
            _candidate_id(self.candidate_id)
            if self.release_id != f"{self.strategy_family_id}-{self.candidate_id}":
                raise RuntimeContractError(
                    "candidate runtime reference differs from candidate identity"
                )
        else:
            raise RuntimeContractError("runtime identity_kind must be RELEASE or CANDIDATE")
        if not _SHA256.fullmatch(self.release_hash):
            raise RuntimeContractError("release_hash must be lowercase SHA-256")
        required_datasets = {item.dataset for item in self.inputs.requirements}
        if not required_datasets.issubset(self.capabilities.datasets):
            raise RuntimeContractError("input datasets must be declared as required capabilities")
        if self.state_mode not in {"STATELESS", "PERSISTED"}:
            raise RuntimeContractError("runtime state_mode must be STATELESS or PERSISTED")

    @property
    def runtime_sha256(self) -> str:
        identity = {
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
        # Keep the five existing frozen identities byte-for-byte stable.
        if self.schema_version == 2:
            identity.update(identity_kind=self.identity_kind, candidate_id=self.candidate_id)
            identity["contracts"] = {
                "inputs": [
                    {
                        "name": item.name,
                        "dataset": item.dataset,
                        "subject": item.subject,
                        "frequency": item.frequency,
                        "lookback_sessions": item.lookback_sessions,
                        "cutoff_rule": item.cutoff_rule.value,
                        "maximum_staleness_days": item.maximum_staleness_days,
                    }
                    for item in self.inputs.requirements
                ],
                "decision": {
                    "output_kind": self.decision.output_kind,
                    "minimum_target": self.decision.minimum_target,
                    "maximum_target": self.decision.maximum_target,
                    "effective_time_rule": self.decision.effective_time_rule,
                },
                "execution": {
                    "policy_type": self.execution.policy_type,
                    "settings": self.execution.settings,
                },
                "monitoring": {
                    "policy_type": self.monitoring.policy_type,
                    "rules": self.monitoring.rules,
                },
                "capabilities": {
                    "datasets": self.capabilities.datasets,
                    "order_types": self.capabilities.order_types,
                    "checkpoints": self.capabilities.checkpoints,
                },
                "state_mode": self.state_mode,
                "history": {
                    "mode": self.history.mode,
                    "canonical_start": self.history.canonical_start,
                    "required_input_start": self.history.required_input_start,
                },
            }
        return canonical_sha256(identity)


def _candidate_id(value: str | None) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", value):
        raise RuntimeContractError("candidate_id must be a safe non-empty identifier")
    if _VERSION.fullmatch(value):
        raise RuntimeContractError("candidate_id cannot be a frozen version")
    return value


@dataclass(frozen=True, slots=True)
class StrategyCandidate:
    """Immutable research runtime input; lifecycle authority remains with SM.

    Researchers construct a new value for each parameter set. No SM registration
    or frozen version number is needed to run a trial. ``runtime_identity_sha256``
    identifies executable content, not the wider SM submission/claims snapshot.
    """

    strategy_family_id: str
    candidate_id: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not _FAMILY_ID.fullmatch(self.strategy_family_id):
            raise RuntimeContractError("strategy_family_id must look like S001")
        _candidate_id(self.candidate_id)
        object.__setattr__(self, "payload", _json_mapping(self.payload, "candidate payload"))
        if not isinstance(self.payload.get("runtime"), Mapping):
            raise RuntimeContractError("candidate payload must declare its runtime implementation")
        if not isinstance(self.payload.get("parameters"), Mapping):
            raise RuntimeContractError("candidate payload must declare its parameter values")

    @property
    def reference_id(self) -> str:
        return f"{self.strategy_family_id}-{self.candidate_id}"

    @property
    def runtime_identity_sha256(self) -> str:
        return canonical_sha256(
            {
                "strategy_family_id": self.strategy_family_id,
                "candidate_id": self.candidate_id,
                "payload": self.payload,
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
        if raw.get("schema_version") in {2, 3}:
            release_payload = {
                "schema_version": raw["schema_version"],
                "strategy_id": raw["strategy_id"],
                "version": raw["version"],
                "release_id": raw["release_id"],
                "strategy_payload": raw["strategy_payload"],
            }
        else:
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


def _daily_prices(frame: pd.DataFrame, field_name: str) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise RuntimeContractError(f"{field_name} must be a dataframe")
    normalized = frame.rename(
        columns={
            "Date": "dt",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "vol",
            "Amount": "amount",
        }
    ).copy()
    if not {"dt", "close"} <= set(normalized.columns):
        raise RuntimeContractError(f"{field_name} must contain dt and close")
    try:
        normalized["dt"] = pd.to_datetime(normalized["dt"], errors="raise").dt.normalize()
        normalized["close"] = pd.to_numeric(normalized["close"], errors="raise")
    except (TypeError, ValueError) as exc:
        raise RuntimeContractError(f"{field_name} has invalid dates or prices") from exc
    if (
        normalized.empty
        or normalized["dt"].isna().any()
        or normalized["dt"].duplicated().any()
        or normalized["close"].map(lambda value: math.isfinite(float(value)) and value > 0).eq(False).any()
    ):
        raise RuntimeContractError(
            f"{field_name} requires unique sessions and positive finite closes"
        )
    return normalized.sort_values("dt").reset_index(drop=True)


@dataclass(frozen=True, slots=True)
class ExecutionPricingData:
    """SRT-owned market facts used to price one strategy decision."""

    symbol: str
    adjusted_daily: pd.DataFrame
    execution_daily: pd.DataFrame

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _text(self.symbol, "pricing symbol").upper())
        adjusted = _daily_prices(self.adjusted_daily, "adjusted daily prices")
        execution = _daily_prices(self.execution_daily, "execution daily prices")
        adjusted_sessions = pd.DatetimeIndex(adjusted["dt"])
        execution_sessions = pd.DatetimeIndex(execution["dt"])
        if not adjusted_sessions.equals(execution_sessions):
            raise RuntimeContractError("adjusted and execution pricing sessions differ")
        object.__setattr__(self, "adjusted_daily", adjusted)
        object.__setattr__(self, "execution_daily", execution)

    @property
    def data_cutoff(self) -> date:
        return pd.Timestamp(self.execution_daily.iloc[-1]["dt"]).date()

    @property
    def identity_hashes(self) -> Mapping[str, str]:
        from dataflows import canonical_frame_sha256

        return MappingProxyType(
            {
                "adjusted_daily": canonical_frame_sha256(self.adjusted_daily),
                "execution_daily": canonical_frame_sha256(self.execution_daily),
            }
        )

    def references_for(self, decision: "StrategyDecision") -> "ReferencePriceSnapshot":
        raw_signal_date = decision.evidence.get("signal_date")
        if raw_signal_date is None:
            raise RuntimeContractError("strategy decision must declare signal_date")
        try:
            signal_date = pd.Timestamp(raw_signal_date).normalize()
        except (TypeError, ValueError) as exc:
            raise RuntimeContractError("strategy decision signal_date is invalid") from exc
        adjusted = self.adjusted_daily.loc[self.adjusted_daily["dt"].eq(signal_date)]
        execution = self.execution_daily.loc[self.execution_daily["dt"].eq(signal_date)]
        if len(adjusted) != 1 or len(execution) != 1:
            raise RuntimeContractError(
                "execution pricing has no unique reference row for the signal session"
            )
        if decision.valid_at.date() <= signal_date.date():
            raise RuntimeContractError("execution instruction must follow the signal session")
        signal_at = datetime.combine(
            signal_date.date(), time(15, 0), tzinfo=decision.valid_at.tzinfo
        )
        return ReferencePriceSnapshot(
            symbol=self.symbol,
            signal_at=signal_at,
            valid_at=decision.valid_at,
            signal_reference_price=float(adjusted.iloc[0]["close"]),
            execution_reference_price=float(execution.iloc[0]["close"]),
            signal_price_basis="ADJUSTED_CLOSE",
            execution_price_basis="UNADJUSTED_CLOSE",
            price_identity_hashes=self.identity_hashes,
        )


@dataclass(frozen=True, slots=True)
class StrategyRuntimeContext:
    """Complete SRT input: strategy publication plus execution-pricing facts."""

    strategy_data: PublishedStrategyData
    pricing_data: ExecutionPricingData

    def __post_init__(self) -> None:
        if not self.strategy_data.ready:
            raise RuntimeContractError("strategy runtime context requires READY publication")
        try:
            cutoff = date.fromisoformat(self.strategy_data.requested_cutoff)
        except ValueError as exc:
            raise RuntimeContractError("strategy publication cutoff is invalid") from exc
        if cutoff != self.pricing_data.data_cutoff:
            raise RuntimeContractError(
                "strategy publication and execution pricing cutoffs differ"
            )


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
class ReferencePriceSnapshot:
    symbol: str
    signal_at: datetime
    valid_at: datetime
    signal_reference_price: float
    execution_reference_price: float
    signal_price_basis: str
    execution_price_basis: str
    price_identity_hashes: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _text(self.symbol, "reference symbol").upper())
        if self.signal_at.tzinfo is None or self.valid_at.tzinfo is None:
            raise RuntimeContractError("reference timestamps must be timezone-aware")
        if self.valid_at <= self.signal_at:
            raise RuntimeContractError("reference valid_at must follow signal_at")
        prices = (self.signal_reference_price, self.execution_reference_price)
        if not all(math.isfinite(value) and value > 0 for value in prices):
            raise RuntimeContractError("reference prices must be positive and finite")
        object.__setattr__(
            self, "signal_price_basis", _text(self.signal_price_basis, "signal price basis")
        )
        object.__setattr__(
            self,
            "execution_price_basis",
            _text(self.execution_price_basis, "execution price basis"),
        )
        identities = _mapping(self.price_identity_hashes, "price_identity_hashes")
        if not identities or any(not _SHA256.fullmatch(value) for value in identities.values()):
            raise RuntimeContractError("price identities must be lowercase SHA-256")
        object.__setattr__(self, "price_identity_hashes", identities)


@dataclass(frozen=True, slots=True)
class ExecutionInstruction:
    decision: StrategyDecision
    policy: ExecutionPolicy
    reference_prices: ReferencePriceSnapshot
    order_plan: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.reference_prices.valid_at != self.decision.valid_at:
            raise RuntimeContractError("instruction and decision effective times differ")
        signal_date = self.decision.evidence.get("signal_date")
        if signal_date is None or pd.Timestamp(signal_date).date() != self.reference_prices.signal_at.date():
            raise RuntimeContractError("instruction and decision signal sessions differ")
        plan = _json_mapping(self.order_plan, "execution order plan")
        if not plan:
            raise RuntimeContractError("execution instruction requires an order plan")
        if float(plan.get("target_position", math.nan)) != self.decision.target_position:
            raise RuntimeContractError("execution plan target differs from strategy decision")
        object.__setattr__(self, "order_plan", plan)

    def order_plan_payload(self) -> dict[str, Any]:
        """Return a detached JSON-compatible order plan for execution adapters."""

        return _thaw_json(self.order_plan)


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    deployment: DeploymentSpec
    account: AccountSnapshot
    instruction: ExecutionInstruction

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
        if self.deployment.symbol.upper() != self.reference_prices.symbol:
            raise RuntimeContractError("execution deployment and reference symbols differ")

    @property
    def decision(self) -> StrategyDecision:
        return self.instruction.decision

    @property
    def policy(self) -> ExecutionPolicy:
        return self.instruction.policy

    @property
    def reference_prices(self) -> ReferencePriceSnapshot:
        return self.instruction.reference_prices

    @property
    def order_plan(self) -> Mapping[str, Any]:
        return self.instruction.order_plan


@dataclass(frozen=True, slots=True)
class RuntimeRunResult:
    status: RuntimeRunStatus
    publication: PublishedStrategyData
    decision: StrategyDecision | None = None
    instruction: ExecutionInstruction | None = None
    receipt: ExecutionReceipt | None = None

    def __post_init__(self) -> None:
        if self.status is RuntimeRunStatus.DATA_NOT_READY:
            if (
                self.publication.ready
                or self.decision is not None
                or self.instruction is not None
                or self.receipt is not None
            ):
                raise RuntimeContractError("DATA_NOT_READY result cannot contain execution data")
            return
        if (
            not self.publication.ready
            or self.decision is None
            or self.instruction is None
            or self.receipt is None
        ):
            raise RuntimeContractError(
                "accepted or rejected runtime result requires publication and execution"
            )
        if self.status is RuntimeRunStatus.ACCEPTED and not self.receipt.accepted:
            raise RuntimeContractError("ACCEPTED result requires an accepted receipt")
        if self.status is RuntimeRunStatus.REJECTED and self.receipt.accepted:
            raise RuntimeContractError("REJECTED result requires a rejected receipt")
