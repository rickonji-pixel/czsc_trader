from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, ClassVar

from .errors import ValidationError
from .validation import (
    require_date,
    require_exact_fields,
    require_identifier,
    require_number,
    require_schema_version,
    require_sha256,
    require_strategy_id,
    require_string,
    require_timestamp,
    require_version,
)


class Qualification(str, Enum):
    RESEARCH = "RESEARCH"
    PAPER_READY = "PAPER_READY"
    LIVE_READY = "LIVE_READY"
    RETIRED = "RETIRED"


class EvidencePhase(str, Enum):
    RESEARCH_BACKTEST = "RESEARCH_BACKTEST"
    PAPER_FORWARD = "PAPER_FORWARD"
    LIVE = "LIVE"


class ResearchState(str, Enum):
    RESEARCHING = "RESEARCHING"
    PAUSED = "PAUSED"
    TERMINATED = "TERMINATED"


class ReviewStatus(str, Enum):
    OPEN = "OPEN"
    EVALUATING = "EVALUATING"
    ELIGIBLE = "ELIGIBLE"
    INCOMPLETE = "INCOMPLETE"
    REJECTED = "REJECTED"
    INVALIDATED = "INVALIDATED"
    FROZEN = "FROZEN"


@dataclass(frozen=True)
class FreezeApproval:
    schema_version: int
    assessment_id: str
    strategy_id: str
    candidate_id: str
    candidate_hash: str
    decision: str
    risk_label: str
    source_experiment: str
    machine_report_id: str
    machine_report_hash: str
    machine_verdict: str
    reviewed_by: str
    rationale: str
    mechanism_review: str
    external_relevance_review: str
    deployment_review: str
    monitoring_plan_review: str
    reviewed_at: str

    FIELDS: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "assessment_id",
        "strategy_id",
        "candidate_id",
        "candidate_hash",
        "decision",
        "risk_label",
        "source_experiment",
        "machine_report_id",
        "machine_report_hash",
        "machine_verdict",
        "reviewed_by",
        "rationale",
        "mechanism_review",
        "external_relevance_review",
        "deployment_review",
        "monitoring_plan_review",
        "reviewed_at",
    )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> FreezeApproval:
        require_exact_fields(value, cls.FIELDS)
        if value["schema_version"] != 2 or isinstance(value["schema_version"], bool):
            raise ValidationError("new freeze approval schema_version must be 2")
        decision = require_string(value["decision"], "decision")
        if decision != "APPROVE_FREEZE":
            raise ValidationError("freeze approval decision must be APPROVE_FREEZE")
        risk_label = require_string(value["risk_label"], "risk_label")
        if risk_label not in {"FAVORABLE", "MIXED"}:
            raise ValidationError("freeze approval risk_label must be FAVORABLE or MIXED")
        machine_verdict = require_string(value["machine_verdict"], "machine_verdict")
        if machine_verdict != "ELIGIBLE_FOR_FREEZE_REVIEW":
            raise ValidationError(
                "freeze approval requires ELIGIBLE_FOR_FREEZE_REVIEW machine verdict"
            )
        reviews = {
            name: require_string(value[name], name)
            for name in (
                "mechanism_review",
                "external_relevance_review",
                "deployment_review",
                "monitoring_plan_review",
            )
        }
        if any(item != "APPROVED" for item in reviews.values()):
            raise ValidationError("all human freeze review items must be APPROVED")
        return cls(
            schema_version=2,
            assessment_id=require_string(value["assessment_id"], "assessment_id"),
            strategy_id=require_strategy_id(value["strategy_id"]),
            candidate_id=require_string(value["candidate_id"], "candidate_id"),
            candidate_hash=require_sha256(value["candidate_hash"], "candidate_hash"),
            decision=decision,
            risk_label=risk_label,
            source_experiment=require_string(value["source_experiment"], "source_experiment"),
            machine_report_id=require_identifier(value["machine_report_id"], "machine_report_id"),
            machine_report_hash=require_sha256(
                value["machine_report_hash"], "machine_report_hash"
            ),
            machine_verdict=machine_verdict,
            reviewed_by=require_string(value["reviewed_by"], "reviewed_by"),
            rationale=require_string(value["rationale"], "rationale"),
            mechanism_review=reviews["mechanism_review"],
            external_relevance_review=reviews["external_relevance_review"],
            deployment_review=reviews["deployment_review"],
            monitoring_plan_review=reviews["monitoring_plan_review"],
            reviewed_at=require_timestamp(value["reviewed_at"], "reviewed_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _enum_dict(instance: object) -> dict[str, Any]:
    return {
        key: value.value if isinstance(value, Enum) else value
        for key, value in asdict(instance).items()
    }


def _enum(enum_type: type[Enum], value: Any, field: str) -> Enum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} has unsupported value: {value!r}") from exc


@dataclass(frozen=True)
class StrategyFamily:
    schema_version: int
    strategy_id: str
    name: str
    scope: Any
    research_intent: dict[str, Any]
    research_state: ResearchState
    created_at: str
    created_by: str
    updated_at: str

    FIELDS: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "strategy_id",
        "name",
        "scope",
        "research_intent",
        "research_state",
        "created_at",
        "created_by",
        "updated_at",
    )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> StrategyFamily:
        require_exact_fields(value, cls.FIELDS)
        if value["schema_version"] != 2 or isinstance(value["schema_version"], bool):
            raise ValidationError("strategy family schema_version must be 2")
        scope = value["scope"]
        if not isinstance(scope, (str, list, dict)) or not scope:
            raise ValidationError("scope must be a nonempty JSON string, list, or object")
        intent = value["research_intent"]
        if not isinstance(intent, dict) or not intent:
            raise ValidationError("research_intent must be a nonempty JSON object")
        return cls(
            schema_version=2,
            strategy_id=require_strategy_id(value["strategy_id"]),
            name=require_string(value["name"], "name"),
            scope=scope,
            research_intent=dict(intent),
            research_state=_enum(
                ResearchState, value["research_state"], "research_state"
            ),
            created_at=require_timestamp(value["created_at"], "created_at"),
            created_by=require_string(value["created_by"], "created_by"),
            updated_at=require_timestamp(value["updated_at"], "updated_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return _enum_dict(self)


@dataclass(frozen=True)
class CandidateSnapshot:
    schema_version: int
    strategy_id: str
    candidate_id: str
    source_experiment: str
    strategy_payload: dict[str, Any]
    data_contract: dict[str, Any]
    execution_policy: dict[str, Any]
    research_claims: dict[str, Any]
    candidate_hash: str

    FIELDS: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "strategy_id",
        "candidate_id",
        "source_experiment",
        "strategy_payload",
        "data_contract",
        "execution_policy",
        "research_claims",
        "candidate_hash",
    )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CandidateSnapshot:
        require_exact_fields(value, cls.FIELDS)
        for field in (
            "strategy_payload",
            "data_contract",
            "execution_policy",
            "research_claims",
        ):
            if not isinstance(value[field], dict) or not value[field]:
                raise ValidationError(f"{field} must be a nonempty JSON object")
        instance = cls(
            schema_version=require_schema_version(value["schema_version"]),
            strategy_id=require_strategy_id(value["strategy_id"]),
            candidate_id=require_identifier(value["candidate_id"], "candidate_id"),
            source_experiment=require_string(
                value["source_experiment"], "source_experiment"
            ),
            strategy_payload=dict(value["strategy_payload"]),
            data_contract=dict(value["data_contract"]),
            execution_policy=dict(value["execution_policy"]),
            research_claims=dict(value["research_claims"]),
            candidate_hash=require_sha256(value["candidate_hash"], "candidate_hash"),
        )
        if instance.candidate_hash != canonical_sha256(instance.hash_payload()):
            raise ValidationError("candidate_hash does not match the candidate snapshot")
        return instance

    def hash_payload(self) -> dict[str, Any]:
        value = self.to_dict()
        value.pop("candidate_hash")
        return value

    def to_dict(self) -> dict[str, Any]:
        return _enum_dict(self)


@dataclass(frozen=True)
class EvaluationMandate:
    schema_version: int
    mandate_id: str
    strategy_id: str
    candidate_id: str
    development_cutoff: str
    forward_start: str
    evaluation_windows: dict[str, Any]
    benchmark: dict[str, Any]
    objectives: list[dict[str, Any]]
    cost_policy: dict[str, Any]
    frequency_policy: dict[str, Any]
    required_audits: list[str]
    evidence_seen_through: str
    finalized_at: str
    finalized_by: str
    mandate_hash: str

    FIELDS: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "mandate_id",
        "strategy_id",
        "candidate_id",
        "development_cutoff",
        "forward_start",
        "evaluation_windows",
        "benchmark",
        "objectives",
        "cost_policy",
        "frequency_policy",
        "required_audits",
        "evidence_seen_through",
        "finalized_at",
        "finalized_by",
        "mandate_hash",
    )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EvaluationMandate:
        require_exact_fields(value, cls.FIELDS)
        for field in ("evaluation_windows", "benchmark", "cost_policy", "frequency_policy"):
            if not isinstance(value[field], dict) or not value[field]:
                raise ValidationError(f"{field} must be a nonempty JSON object")
        objectives = value["objectives"]
        if not isinstance(objectives, list) or not objectives or any(
            not isinstance(item, dict) or not item for item in objectives
        ):
            raise ValidationError("objectives must be a nonempty list of JSON objects")
        required_audits = value["required_audits"]
        if not isinstance(required_audits, list) or not required_audits or any(
            not isinstance(item, str) or not item.strip() for item in required_audits
        ):
            raise ValidationError("required_audits must be a nonempty list of names")
        if len(set(required_audits)) != len(required_audits):
            raise ValidationError("required_audits must not contain duplicates")
        instance = cls(
            schema_version=require_schema_version(value["schema_version"]),
            mandate_id=require_identifier(value["mandate_id"], "mandate_id"),
            strategy_id=require_strategy_id(value["strategy_id"]),
            candidate_id=require_identifier(value["candidate_id"], "candidate_id"),
            development_cutoff=require_date(
                value["development_cutoff"], "development_cutoff"
            ),
            forward_start=require_date(value["forward_start"], "forward_start"),
            evaluation_windows=dict(value["evaluation_windows"]),
            benchmark=dict(value["benchmark"]),
            objectives=[dict(item) for item in objectives],
            cost_policy=dict(value["cost_policy"]),
            frequency_policy=dict(value["frequency_policy"]),
            required_audits=[item.strip() for item in required_audits],
            evidence_seen_through=require_date(
                value["evidence_seen_through"], "evidence_seen_through"
            ),
            finalized_at=require_timestamp(value["finalized_at"], "finalized_at"),
            finalized_by=require_string(value["finalized_by"], "finalized_by"),
            mandate_hash=require_sha256(value["mandate_hash"], "mandate_hash"),
        )
        if instance.mandate_hash != canonical_sha256(instance.hash_payload()):
            raise ValidationError("mandate_hash does not match the evaluation mandate")
        if instance.forward_start <= instance.development_cutoff:
            raise ValidationError("forward_start must be after development_cutoff")
        if instance.evidence_seen_through > instance.development_cutoff:
            raise ValidationError(
                "evidence_seen_through must not exceed development_cutoff"
            )
        return instance

    def hash_payload(self) -> dict[str, Any]:
        value = self.to_dict()
        value.pop("mandate_hash")
        return value

    def to_dict(self) -> dict[str, Any]:
        return _enum_dict(self)


@dataclass(frozen=True)
class FreezeReviewCase:
    schema_version: int
    review_id: str
    strategy_id: str
    candidate_id: str
    candidate_hash: str
    candidate_snapshot_hash: str
    evaluation_mandate_hash: str
    audit_policy_hash: str
    status: ReviewStatus
    adjudication_report_hash: str | None
    opened_at: str
    opened_by: str
    updated_at: str
    invalidation_reason: str | None

    FIELDS: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "review_id",
        "strategy_id",
        "candidate_id",
        "candidate_hash",
        "candidate_snapshot_hash",
        "evaluation_mandate_hash",
        "audit_policy_hash",
        "status",
        "adjudication_report_hash",
        "opened_at",
        "opened_by",
        "updated_at",
        "invalidation_reason",
    )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> FreezeReviewCase:
        require_exact_fields(value, cls.FIELDS)
        reason = value["invalidation_reason"]
        if reason is not None:
            reason = require_string(reason, "invalidation_reason")
        return cls(
            schema_version=require_schema_version(value["schema_version"]),
            review_id=require_identifier(value["review_id"], "review_id"),
            strategy_id=require_strategy_id(value["strategy_id"]),
            candidate_id=require_identifier(value["candidate_id"], "candidate_id"),
            candidate_hash=require_sha256(value["candidate_hash"], "candidate_hash"),
            candidate_snapshot_hash=require_sha256(
                value["candidate_snapshot_hash"], "candidate_snapshot_hash"
            ),
            evaluation_mandate_hash=require_sha256(
                value["evaluation_mandate_hash"], "evaluation_mandate_hash"
            ),
            audit_policy_hash=require_sha256(
                value["audit_policy_hash"], "audit_policy_hash"
            ),
            status=_enum(ReviewStatus, value["status"], "status"),
            adjudication_report_hash=require_sha256(
                value["adjudication_report_hash"],
                "adjudication_report_hash",
                allow_none=True,
            ),
            opened_at=require_timestamp(value["opened_at"], "opened_at"),
            opened_by=require_string(value["opened_by"], "opened_by"),
            updated_at=require_timestamp(value["updated_at"], "updated_at"),
            invalidation_reason=reason,
        )

    def to_dict(self) -> dict[str, Any]:
        return _enum_dict(self)


@dataclass(frozen=True)
class AdjudicationReport:
    schema_version: int
    report_id: str
    review_id: str
    strategy_id: str
    candidate_id: str
    candidate_hash: str
    evaluation_mandate_hash: str
    audit_policy_hash: str
    claim_checks: list[dict[str, Any]]
    audit_results: dict[str, dict[str, Any]]
    machine_verdict: str
    risk_label: str
    blocking_findings: list[str]
    reservations: list[str]
    generated_at: str
    report_hash: str

    FIELDS: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "report_id",
        "review_id",
        "strategy_id",
        "candidate_id",
        "candidate_hash",
        "evaluation_mandate_hash",
        "audit_policy_hash",
        "claim_checks",
        "audit_results",
        "machine_verdict",
        "risk_label",
        "blocking_findings",
        "reservations",
        "generated_at",
        "report_hash",
    )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AdjudicationReport:
        require_exact_fields(value, cls.FIELDS)
        checks = value["claim_checks"]
        audits = value["audit_results"]
        if not isinstance(checks, list) or any(not isinstance(item, dict) for item in checks):
            raise ValidationError("claim_checks must be a list of JSON objects")
        if not isinstance(audits, dict) or any(
            not isinstance(name, str) or not isinstance(item, dict)
            for name, item in audits.items()
        ):
            raise ValidationError("audit_results must map names to JSON objects")
        findings = value["blocking_findings"]
        reservations = value["reservations"]
        if any(
            not isinstance(items, list)
            or any(not isinstance(item, str) or not item for item in items)
            for items in (findings, reservations)
        ):
            raise ValidationError("findings and reservations must be lists of strings")
        verdict = require_identifier(value["machine_verdict"], "machine_verdict")
        if verdict not in {"ELIGIBLE_FOR_FREEZE_REVIEW", "INCOMPLETE", "REJECTED"}:
            raise ValidationError("machine_verdict has unsupported value")
        instance = cls(
            schema_version=require_schema_version(value["schema_version"]),
            report_id=require_identifier(value["report_id"], "report_id"),
            review_id=require_identifier(value["review_id"], "review_id"),
            strategy_id=require_strategy_id(value["strategy_id"]),
            candidate_id=require_identifier(value["candidate_id"], "candidate_id"),
            candidate_hash=require_sha256(value["candidate_hash"], "candidate_hash"),
            evaluation_mandate_hash=require_sha256(
                value["evaluation_mandate_hash"], "evaluation_mandate_hash"
            ),
            audit_policy_hash=require_sha256(
                value["audit_policy_hash"], "audit_policy_hash"
            ),
            claim_checks=[dict(item) for item in checks],
            audit_results={str(name): dict(item) for name, item in audits.items()},
            machine_verdict=verdict,
            risk_label=require_identifier(value["risk_label"], "risk_label"),
            blocking_findings=list(findings),
            reservations=list(reservations),
            generated_at=require_timestamp(value["generated_at"], "generated_at"),
            report_hash=require_sha256(value["report_hash"], "report_hash"),
        )
        if instance.report_hash != canonical_sha256(instance.hash_payload()):
            raise ValidationError("report_hash does not match the adjudication report")
        return instance

    def hash_payload(self) -> dict[str, Any]:
        value = self.to_dict()
        value.pop("report_hash")
        return value

    def to_dict(self) -> dict[str, Any]:
        return _enum_dict(self)


@dataclass(frozen=True)
class StrategyVersion:
    schema_version: int
    strategy_id: str
    version: str
    release_id: str
    parent_version: str | None
    change_summary: str
    source_experiment: str
    source_candidate: int | str | None
    selection_data_cutoff: str
    forward_start: str
    strategy_payload: dict[str, Any]
    release_hash: str | None
    governance: dict[str, str] | None = None
    governance_hash: str | None = None

    FIELDS: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "strategy_id",
        "version",
        "release_id",
        "parent_version",
        "change_summary",
        "source_experiment",
        "source_candidate",
        "selection_data_cutoff",
        "forward_start",
        "strategy_payload",
        "release_hash",
    )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> StrategyVersion:
        schema_version = value.get("schema_version")
        optional = ("governance", "governance_hash") if schema_version == 2 else ()
        require_exact_fields(value, cls.FIELDS, optional)
        if schema_version not in {1, 2} or isinstance(schema_version, bool):
            raise ValidationError("strategy version schema_version must be 1 or 2")
        strategy_id = require_strategy_id(value["strategy_id"])
        version = require_version(value["version"])
        release_id = require_string(value["release_id"], "release_id")
        if release_id != f"{strategy_id}-{version}":
            raise ValidationError("release_id must equal strategy_id-version")
        parent = value["parent_version"]
        if parent is not None:
            parent = require_version(parent)
        payload = value["strategy_payload"]
        if not isinstance(payload, dict) or not payload:
            raise ValidationError("strategy_payload must be a nonempty JSON object")
        governance = value.get("governance")
        governance_hash = value.get("governance_hash")
        if schema_version == 2:
            if not isinstance(governance, dict):
                raise ValidationError("schema v2 strategy version requires governance")
            required_governance = {
                "review_id",
                "candidate_snapshot_hash",
                "evaluation_mandate_hash",
                "adjudication_report_hash",
                "human_decision_hash",
                "runtime_acceptance_hash",
            }
            if set(governance) != required_governance:
                raise ValidationError("strategy version governance fields are incomplete")
            governance = {
                "review_id": require_identifier(governance["review_id"], "review_id"),
                **{
                    name: require_sha256(governance[name], name)
                    for name in required_governance - {"review_id"}
                },
            }
            governance_hash = require_sha256(
                governance_hash, "governance_hash"
            )
            if governance_hash != canonical_sha256(governance):
                raise ValidationError("governance_hash does not match governance")
        elif governance is not None or governance_hash is not None:
            raise ValidationError(
                "schema v1 strategy version must not contain governance"
            )
        instance = cls(
            schema_version=int(schema_version),
            strategy_id=strategy_id,
            version=version,
            release_id=release_id,
            parent_version=parent,
            change_summary=require_string(value["change_summary"], "change_summary"),
            source_experiment=require_string(value["source_experiment"], "source_experiment"),
            source_candidate=value["source_candidate"],
            selection_data_cutoff=require_date(
                value["selection_data_cutoff"], "selection_data_cutoff"
            ),
            forward_start=require_date(value["forward_start"], "forward_start"),
            strategy_payload=payload,
            release_hash=require_sha256(value["release_hash"], "release_hash", allow_none=True),
            governance=governance,
            governance_hash=governance_hash,
        )
        if instance.release_hash and instance.release_hash != canonical_sha256(
            instance.release_payload()
        ):
            raise ValidationError("release_hash does not match the release payload")
        return instance

    def release_payload(self) -> dict[str, Any]:
        if self.schema_version == 2:
            return {
                "schema_version": 2,
                "strategy_id": self.strategy_id,
                "version": self.version,
                "release_id": self.release_id,
                "strategy_payload": self.strategy_payload,
            }
        value = self.to_dict()
        value.pop("release_hash")
        return value

    def to_dict(self) -> dict[str, Any]:
        value = _enum_dict(self)
        if self.schema_version == 1:
            value.pop("governance")
            value.pop("governance_hash")
        return value


@dataclass(frozen=True)
class LifecycleEvent:
    schema_version: int
    event_id: str
    event_type: str
    strategy_id: str
    version: str | None
    from_state: Qualification | None
    to_state: Qualification
    occurred_at: str
    actor: str
    reason: str
    evidence_ids: list[str]
    release_hash: str | None

    FIELDS: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "event_id",
        "event_type",
        "strategy_id",
        "version",
        "from_state",
        "to_state",
        "occurred_at",
        "actor",
        "reason",
        "evidence_ids",
        "release_hash",
    )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LifecycleEvent:
        require_exact_fields(value, cls.FIELDS)
        version = value["version"]
        if version is not None:
            version = require_version(version)
        from_state = value["from_state"]
        evidence_ids = value["evidence_ids"]
        if not isinstance(evidence_ids, list) or any(
            not isinstance(item, str) or not item for item in evidence_ids
        ):
            raise ValidationError("evidence_ids must be a list of nonblank strings")
        return cls(
            schema_version=require_schema_version(value["schema_version"]),
            event_id=require_identifier(value["event_id"], "event_id"),
            event_type=require_identifier(value["event_type"], "event_type"),
            strategy_id=require_strategy_id(value["strategy_id"]),
            version=version,
            from_state=None
            if from_state is None
            else _enum(Qualification, from_state, "from_state"),
            to_state=_enum(Qualification, value["to_state"], "to_state"),
            occurred_at=require_timestamp(value["occurred_at"], "occurred_at"),
            actor=require_string(value["actor"], "actor"),
            reason=require_string(value["reason"], "reason"),
            evidence_ids=list(evidence_ids),
            release_hash=require_sha256(value["release_hash"], "release_hash", allow_none=True),
        )

    def to_dict(self) -> dict[str, Any]:
        return _enum_dict(self)


@dataclass(frozen=True)
class PerformanceEvidence:
    schema_version: int
    evidence_id: str
    strategy_id: str
    version: str
    release_hash: str
    phase: EvidencePhase
    period_start: str
    period_end: str
    data_identity: dict[str, Any]
    initial_capital: float
    fee_rate: float
    maximum_drawdown: float
    calmar_ratio: float
    win_loss_ratio: float | None
    win_loss_ratio_status: str
    total_return: float
    sharpe_ratio: float | None
    closed_trades: int
    source_path: str
    source_hash: str
    recorded_at: str
    recorded_by: str

    FIELDS: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "evidence_id",
        "strategy_id",
        "version",
        "release_hash",
        "phase",
        "period_start",
        "period_end",
        "data_identity",
        "initial_capital",
        "fee_rate",
        "maximum_drawdown",
        "calmar_ratio",
        "win_loss_ratio",
        "win_loss_ratio_status",
        "total_return",
        "sharpe_ratio",
        "closed_trades",
        "source_path",
        "source_hash",
        "recorded_at",
        "recorded_by",
    )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PerformanceEvidence:
        require_exact_fields(value, cls.FIELDS)
        data_identity = value["data_identity"]
        if not isinstance(data_identity, dict) or not data_identity:
            raise ValidationError("data_identity must be a nonempty JSON object")
        closed_trades = value["closed_trades"]
        if isinstance(closed_trades, bool) or not isinstance(closed_trades, int) or closed_trades < 0:
            raise ValidationError("closed_trades must be a nonnegative integer")
        win_loss_ratio = value["win_loss_ratio"]
        sharpe_ratio = value["sharpe_ratio"]
        instance = cls(
            schema_version=require_schema_version(value["schema_version"]),
            evidence_id=require_identifier(value["evidence_id"], "evidence_id"),
            strategy_id=require_strategy_id(value["strategy_id"]),
            version=require_version(value["version"]),
            release_hash=require_sha256(value["release_hash"], "release_hash"),
            phase=_enum(EvidencePhase, value["phase"], "phase"),
            period_start=require_date(value["period_start"], "period_start"),
            period_end=require_date(value["period_end"], "period_end"),
            data_identity=data_identity,
            initial_capital=require_number(value["initial_capital"], "initial_capital", minimum=0),
            fee_rate=require_number(value["fee_rate"], "fee_rate", minimum=0),
            maximum_drawdown=require_number(value["maximum_drawdown"], "maximum_drawdown"),
            calmar_ratio=require_number(value["calmar_ratio"], "calmar_ratio"),
            win_loss_ratio=None
            if win_loss_ratio is None
            else require_number(win_loss_ratio, "win_loss_ratio", minimum=0),
            win_loss_ratio_status=require_identifier(
                value["win_loss_ratio_status"], "win_loss_ratio_status"
            ),
            total_return=require_number(value["total_return"], "total_return"),
            sharpe_ratio=None
            if sharpe_ratio is None
            else require_number(sharpe_ratio, "sharpe_ratio"),
            closed_trades=closed_trades,
            source_path=require_string(value["source_path"], "source_path"),
            source_hash=require_sha256(value["source_hash"], "source_hash"),
            recorded_at=require_timestamp(value["recorded_at"], "recorded_at"),
            recorded_by=require_string(value["recorded_by"], "recorded_by"),
        )
        if instance.period_end < instance.period_start:
            raise ValidationError("period_end must not precede period_start")
        return instance

    def to_dict(self) -> dict[str, Any]:
        return _enum_dict(self)
