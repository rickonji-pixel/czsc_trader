from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .audit_models import AuditStatus, RiskLabel
from .benchmark_challenge import BenchmarkChallengeDecision
from .models import Record


class FreezeHealthDecision(str, Enum):
    RECOMMEND_FREEZE = "RECOMMEND_FREEZE"
    KEEP_RESEARCHING = "KEEP_RESEARCHING"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True)
class FreezeHealthRequest(Record):
    candidate_id: str
    candidate_hash: str
    candidate_readiness: AuditStatus
    benchmark_challenge: BenchmarkChallengeDecision
    evidence_integrity: AuditStatus
    reproducibility: AuditStatus
    technical_replay: AuditStatus
    cost_stress: AuditStatus
    monitoring_plan: AuditStatus
    mechanism_evidence: RiskLabel
    statistical_evidence: RiskLabel
    parameter_robustness: RiskLabel
    external_validation: RiskLabel
    execution_evidence: RiskLabel
    blocking_findings: tuple[str, ...] = ()


@dataclass(frozen=True)
class FreezeHealthResult(Record):
    candidate_id: str
    candidate_hash: str
    decision: FreezeHealthDecision
    risk_label: RiskLabel
    reason_codes: tuple[str, ...]


def assess_freeze_health(request: FreezeHealthRequest) -> FreezeHealthResult:
    """Run the final SE gate after candidate registration and opponent PK."""
    if not request.candidate_id.strip() or not request.candidate_hash.strip():
        raise ValueError("candidate identity must be nonblank")

    required_audits = (
        request.candidate_readiness,
        request.evidence_integrity,
        request.reproducibility,
        request.technical_replay,
        request.cost_stress,
        request.monitoring_plan,
    )
    risk_evidence = (
        request.mechanism_evidence,
        request.statistical_evidence,
        request.parameter_robustness,
        request.external_validation,
        request.execution_evidence,
    )
    reasons: list[str] = []
    if request.benchmark_challenge is not BenchmarkChallengeDecision.RECOMMEND_HEALTH_CHECK:
        reasons.append("BENCHMARK_CHALLENGE_NOT_PASSED")
    if AuditStatus.FAIL in required_audits:
        reasons.append("REQUIRED_AUDIT_FAILED")
    if RiskLabel.WEAK in risk_evidence:
        reasons.append("WEAK_EVIDENCE")
    if request.blocking_findings:
        reasons.append("BLOCKING_FINDINGS")
    if reasons:
        return FreezeHealthResult(
            request.candidate_id,
            request.candidate_hash,
            FreezeHealthDecision.KEEP_RESEARCHING,
            RiskLabel.WEAK,
            tuple(reasons),
        )
    if AuditStatus.INSUFFICIENT in required_audits:
        return FreezeHealthResult(
            request.candidate_id,
            request.candidate_hash,
            FreezeHealthDecision.INSUFFICIENT_EVIDENCE,
            RiskLabel.MIXED,
            ("INCOMPLETE_REQUIRED_AUDIT",),
        )

    risk = (
        RiskLabel.FAVORABLE
        if all(label is RiskLabel.FAVORABLE for label in risk_evidence)
        else RiskLabel.MIXED
    )
    return FreezeHealthResult(
        request.candidate_id,
        request.candidate_hash,
        FreezeHealthDecision.RECOMMEND_FREEZE,
        risk,
        (
            "CANDIDATE_REGISTERED",
            "BENCHMARK_CHALLENGE_PASSED",
            "REQUIRED_AUDITS_PASSED",
            "MONITORING_PLAN_APPROVED",
            "NO_BLOCKING_FINDINGS",
        ),
    )
