from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from .replay_audit import ReplayEvidence

from .models import (
    CandidateDescriptor,
    CandidateProfile,
    MetricObservation,
    Record,
    TrialRecord,
    _exact,
    _json_value,
    _pairs,
)


class AuditStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT = "INSUFFICIENT"


class RiskLabel(str, Enum):
    FAVORABLE = "FAVORABLE"
    MIXED = "MIXED"
    WEAK = "WEAK"


@dataclass(frozen=True)
class AuditIdentity(Record):
    experiment_id: str
    pool_hash: str
    data_cutoff: str
    data_hash: str
    execution_policy_hash: str
    standard_version: str
    audit_protocol_version: str
    seed: int

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AuditIdentity:
        names = {
            "experiment_id", "pool_hash", "data_cutoff", "data_hash",
            "execution_policy_hash", "standard_version", "audit_protocol_version", "seed",
        }
        _exact(data, names)
        return cls(
            str(data["experiment_id"]), str(data["pool_hash"]), str(data["data_cutoff"]),
            str(data["data_hash"]), str(data["execution_policy_hash"]),
            str(data["standard_version"]), str(data["audit_protocol_version"]), int(data["seed"]),
        )


@dataclass(frozen=True)
class ReturnMatrixEvidence(Record):
    dates: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    returns: tuple[tuple[float, ...], ...]
    content_hash: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReturnMatrixEvidence:
        _exact(data, {"dates", "candidate_ids", "returns", "content_hash"})
        return cls(
            tuple(map(str, data["dates"])),
            tuple(map(str, data["candidate_ids"])),
            tuple(tuple(float(value) for value in row) for row in data["returns"]),
            str(data["content_hash"]),
        )


@dataclass(frozen=True)
class ParameterPoint(Record):
    candidate_id: str
    behavior_hash: str
    parameters: tuple[tuple[str, float], ...]
    eligible: bool
    pareto_layer: int | None
    worst_scores: tuple[tuple[str, float], ...]
    is_incumbent: bool = False

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ParameterPoint:
        required = {
            "candidate_id", "behavior_hash", "parameters", "eligible", "pareto_layer",
            "worst_scores",
        }
        _exact(data, required, {"is_incumbent"})
        return cls(
            str(data["candidate_id"]),
            str(data["behavior_hash"]),
            tuple((key, float(value)) for key, value in _pairs(data["parameters"], "parameters")),
            bool(data["eligible"]),
            None if data["pareto_layer"] is None else int(data["pareto_layer"]),
            tuple((key, float(value)) for key, value in _pairs(data["worst_scores"], "worst_scores")),
            bool(data.get("is_incumbent", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "behavior_hash": self.behavior_hash,
            "parameters": dict(self.parameters),
            "eligible": self.eligible,
            "pareto_layer": self.pareto_layer,
            "worst_scores": dict(self.worst_scores),
            "is_incumbent": self.is_incumbent,
        }


@dataclass(frozen=True)
class ExecutionOrder(Record):
    signal_date: str
    execution_date: str
    side: str
    price: float
    size: float
    fees: float
    event_id: str = ""
    is_initial: bool = False

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExecutionOrder:
        required = {"signal_date", "execution_date", "side", "price", "size", "fees"}
        _exact(data, required, {"event_id", "is_initial"})
        return cls(
            str(data["signal_date"]), str(data["execution_date"]), str(data["side"]),
            float(data["price"]), float(data["size"]), float(data["fees"]),
            str(data.get("event_id", "")), bool(data.get("is_initial", False)),
        )


@dataclass(frozen=True)
class FactorEvent(Record):
    event_id: str
    signal_date: str
    event_type: str
    before_position: float
    after_position: float
    factor_score: float | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FactorEvent:
        required = {"event_id", "signal_date", "event_type", "before_position", "after_position"}
        _exact(data, required, {"factor_score"})
        score = data.get("factor_score")
        return cls(
            str(data["event_id"]), str(data["signal_date"]), str(data["event_type"]),
            float(data["before_position"]), float(data["after_position"]),
            None if score is None else float(score),
        )


@dataclass(frozen=True)
class ExecutionEvidence(Record):
    dates: tuple[str, ...]
    target_positions: tuple[float, ...]
    factor_scores: tuple[float, ...]
    events: tuple[FactorEvent, ...]
    orders: tuple[ExecutionOrder, ...]
    content_hash: str
    window_count: int = 1

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExecutionEvidence:
        _exact(
            data,
            {"dates", "target_positions", "factor_scores", "events", "orders", "content_hash"},
            {"window_count"},
        )
        return cls(
            tuple(map(str, data["dates"])),
            tuple(float(value) for value in data["target_positions"]),
            tuple(float(value) for value in data["factor_scores"]),
            tuple(FactorEvent.from_dict(value) for value in data["events"]),
            tuple(ExecutionOrder.from_dict(value) for value in data["orders"]),
            str(data["content_hash"]),
            int(data.get("window_count", 1)),
        )


@dataclass(frozen=True)
class StressScenario(Record):
    scenario_id: str
    fee_multiplier: float
    slippage_bp: int


@dataclass(frozen=True)
class StressScenarioResult(Record):
    scenario_id: str
    execution_policy_hash: str
    observations: tuple[MetricObservation, ...]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StressScenarioResult:
        _exact(data, {"scenario_id", "execution_policy_hash", "observations"})
        return cls(
            str(data["scenario_id"]),
            str(data["execution_policy_hash"]),
            tuple(MetricObservation.from_dict(value) for value in data["observations"]),
        )


@dataclass(frozen=True)
class AuditFinding(Record):
    audit_id: str
    status: AuditStatus
    reason_codes: tuple[str, ...] = ()
    details: tuple[tuple[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "status": self.status.value,
            "reason_codes": list(self.reason_codes),
            "details": {key: _json_value(value) for key, value in self.details},
        }


@dataclass(frozen=True)
class ChampionAuditRequest(Record):
    identity: AuditIdentity
    champion_id: str
    incumbent_id: str
    pareto_peer_ids: tuple[str, ...]
    search_returns: ReturnMatrixEvidence
    comparison_returns: ReturnMatrixEvidence
    parameters: tuple[ParameterPoint, ...]
    execution: ExecutionEvidence | ReplayEvidence
    formal_observations: tuple[MetricObservation, ...]
    repeated_observations: tuple[MetricObservation, ...]
    candidates: tuple[CandidateDescriptor, ...]
    trials: tuple[TrialRecord, ...]
    profiles: tuple[CandidateProfile, ...]
    stress_results: tuple[StressScenarioResult, ...]
    bootstrap_repetitions: int = 10_000
    bootstrap_block_lengths: tuple[int, ...] = (21, 10, 42)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ChampionAuditRequest:
        from .replay_audit import ReplayEvidence
        required = {
            "identity", "champion_id", "incumbent_id", "pareto_peer_ids",
            "search_returns", "comparison_returns", "parameters", "execution",
            "formal_observations", "repeated_observations", "candidates", "trials",
            "profiles", "stress_results",
        }
        _exact(data, required, {"bootstrap_repetitions", "bootstrap_block_lengths"})

        def profile(value: Mapping[str, Any]) -> CandidateProfile:
            required_profile = {
                "candidate_id", "eligible", "target_achieved", "worst_scores",
                "pareto_layer", "median_score", "turnover", "parameter_distance",
                "reason_codes",
            }
            _exact(value, required_profile)
            return CandidateProfile(
                str(value["candidate_id"]), bool(value["eligible"]),
                bool(value["target_achieved"]),
                tuple((key, float(item)) for key, item in _pairs(value["worst_scores"], "worst_scores")),
                None if value["pareto_layer"] is None else int(value["pareto_layer"]),
                float(value["median_score"]),
                None if value["turnover"] is None else float(value["turnover"]),
                float(value["parameter_distance"]), tuple(map(str, value["reason_codes"])),
            )

        return cls(
            AuditIdentity.from_dict(data["identity"]), str(data["champion_id"]),
            str(data["incumbent_id"]), tuple(map(str, data["pareto_peer_ids"])),
            ReturnMatrixEvidence.from_dict(data["search_returns"]),
            ReturnMatrixEvidence.from_dict(data["comparison_returns"]),
            tuple(ParameterPoint.from_dict(value) for value in data["parameters"]),
            (ReplayEvidence.from_dict(data["execution"])
             if "execution_spec" in data["execution"]
             else ExecutionEvidence.from_dict(data["execution"])),
            tuple(MetricObservation.from_dict(value) for value in data["formal_observations"]),
            tuple(MetricObservation.from_dict(value) for value in data["repeated_observations"]),
            tuple(CandidateDescriptor.from_dict(value) for value in data["candidates"]),
            tuple(TrialRecord.from_dict(value) for value in data["trials"]),
            tuple(profile(value) for value in data["profiles"]),
            tuple(StressScenarioResult.from_dict(value) for value in data["stress_results"]),
            int(data.get("bootstrap_repetitions", 10_000)),
            tuple(int(value) for value in data.get("bootstrap_block_lengths", (21, 10, 42))),
        )


@dataclass(frozen=True)
class ChampionAuditResult(Record):
    identity: AuditIdentity
    candidate_id: str
    status: AuditStatus
    risk_label: RiskLabel | None
    findings: tuple[AuditFinding, ...]
    reason_codes: tuple[str, ...]
    search_bias: Any | None = None
    dsr: Any | None = None
    bootstrap: tuple[Any, ...] = ()
    neighborhood: Any | None = None
    stress: Any | None = None
    direction_flags: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "candidate_id": self.candidate_id,
            "status": self.status.value,
            "risk_label": None if self.risk_label is None else self.risk_label.value,
            "findings": [item.to_dict() for item in self.findings],
            "reason_codes": list(self.reason_codes),
            "search_bias": _json_value(self.search_bias),
            "dsr": _json_value(self.dsr),
            "bootstrap": [_json_value(item) for item in self.bootstrap],
            "neighborhood": _json_value(self.neighborhood),
            "stress": _json_value(self.stress),
            "direction_flags": dict(self.direction_flags),
        }
