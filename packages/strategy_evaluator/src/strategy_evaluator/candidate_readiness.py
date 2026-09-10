from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .audit_models import AuditStatus, RiskLabel
from .models import Record


class CandidateReadinessDecision(str, Enum):
    """SE decision for registering a strategy's first research candidate."""

    RECOMMEND_REGISTRATION = "RECOMMEND_REGISTRATION"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    REJECT = "REJECT"


@dataclass(frozen=True)
class CandidateReadinessRequest(Record):
    candidate_id: str
    candidate_hash: str
    integrity: AuditStatus
    reproducibility: AuditStatus
    mechanism_evidence: RiskLabel
    statistical_evidence: RiskLabel
    external_validation: RiskLabel
    primary_closed_trades: int
    minimum_closed_trades: int
    blocking_findings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateReadinessResult(Record):
    candidate_id: str
    candidate_hash: str
    decision: CandidateReadinessDecision
    risk_label: RiskLabel
    reason_codes: tuple[str, ...]


def assess_research_candidate(
    request: CandidateReadinessRequest,
) -> CandidateReadinessResult:
    """Assess first-candidate readiness without inventing an incumbent.

    MIXED evidence is acceptable at the research-candidate threshold. Freeze and
    deployment remain separate lifecycle decisions.
    """
    if not request.candidate_id.strip() or not request.candidate_hash.strip():
        raise ValueError("candidate identity must be nonblank")
    if request.primary_closed_trades < 0 or request.minimum_closed_trades < 1:
        raise ValueError("trade counts must be nonnegative and minimum must be positive")

    reasons: list[str] = []
    if request.integrity is AuditStatus.FAIL:
        reasons.append("EVIDENCE_INTEGRITY_FAILED")
    if request.reproducibility is AuditStatus.FAIL:
        reasons.append("REPRODUCIBILITY_FAILED")
    if request.blocking_findings:
        reasons.append("BLOCKING_FINDINGS")
    labels = (
        request.mechanism_evidence,
        request.statistical_evidence,
        request.external_validation,
    )
    if RiskLabel.WEAK in labels:
        reasons.append("WEAK_EVIDENCE")
    if reasons:
        return CandidateReadinessResult(
            request.candidate_id,
            request.candidate_hash,
            CandidateReadinessDecision.REJECT,
            RiskLabel.WEAK,
            tuple(reasons),
        )

    if AuditStatus.INSUFFICIENT in {request.integrity, request.reproducibility}:
        reasons.append("INCOMPLETE_AUDIT")
    if request.primary_closed_trades < request.minimum_closed_trades:
        reasons.append("LOW_SAMPLE")
    if reasons:
        return CandidateReadinessResult(
            request.candidate_id,
            request.candidate_hash,
            CandidateReadinessDecision.INSUFFICIENT_EVIDENCE,
            RiskLabel.MIXED,
            tuple(reasons),
        )

    risk = RiskLabel.FAVORABLE if all(
        item is RiskLabel.FAVORABLE for item in labels
    ) else RiskLabel.MIXED
    return CandidateReadinessResult(
        request.candidate_id,
        request.candidate_hash,
        CandidateReadinessDecision.RECOMMEND_REGISTRATION,
        risk,
        (
            "IDENTITY_VERIFIED",
            "EVIDENCE_COMPLETE",
            "MINIMUM_SAMPLE_MET",
            "NO_BLOCKING_FINDINGS",
        ),
    )
