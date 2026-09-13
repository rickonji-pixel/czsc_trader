from __future__ import annotations

from dataclasses import dataclass

from .audit_models import AuditStatus, RiskLabel
from .benchmark_challenge import BenchmarkChallengeDecision
from .bootstrap import PerformanceMetrics
from .models import Record


@dataclass(frozen=True)
class MandateChallengeRequest(Record):
    """Compare a candidate and incumbent against explicit portfolio mandates."""

    candidate_id: str
    candidate_hash: str
    incumbent_id: str
    integrity: AuditStatus
    reproducibility: AuditStatus
    technical_replay: AuditStatus
    candidate_performance: PerformanceMetrics
    incumbent_performance: PerformanceMetrics
    minimum_cagr: float
    maximum_drawdown_floor: float
    blocking_findings: tuple[str, ...] = ()


@dataclass(frozen=True)
class MandateChallengeResult(Record):
    candidate_id: str
    candidate_hash: str
    incumbent_id: str
    decision: BenchmarkChallengeDecision
    risk_label: RiskLabel
    candidate_mandate_passed: bool
    incumbent_mandate_passed: bool
    cagr_cost: float
    drawdown_improvement: float
    calmar_change: float
    reason_codes: tuple[str, ...]


def _passes(performance: PerformanceMetrics, minimum_cagr: float, floor: float) -> bool:
    return performance.cagr >= minimum_cagr and performance.max_drawdown >= floor


def assess_mandate_challenge(request: MandateChallengeRequest) -> MandateChallengeResult:
    """Route a risk-budget tradeoff without weakening ordinary champion PK.

    A candidate may reach health review when it satisfies explicit return and
    drawdown mandates that the incumbent violates. If both satisfy the mandate,
    the ordinary dominance-based benchmark challenge remains required.
    """
    if not all(
        value.strip()
        for value in (request.candidate_id, request.candidate_hash, request.incumbent_id)
    ):
        raise ValueError("candidate and incumbent identity must be nonblank")
    if request.minimum_cagr < 0:
        raise ValueError("minimum CAGR must be nonnegative")
    if not -1 < request.maximum_drawdown_floor <= 0:
        raise ValueError("maximum drawdown floor must be in (-1, 0]")

    candidate_passed = _passes(
        request.candidate_performance,
        request.minimum_cagr,
        request.maximum_drawdown_floor,
    )
    incumbent_passed = _passes(
        request.incumbent_performance,
        request.minimum_cagr,
        request.maximum_drawdown_floor,
    )
    common = {
        "candidate_id": request.candidate_id,
        "candidate_hash": request.candidate_hash,
        "incumbent_id": request.incumbent_id,
        "candidate_mandate_passed": candidate_passed,
        "incumbent_mandate_passed": incumbent_passed,
        "cagr_cost": request.candidate_performance.cagr
        - request.incumbent_performance.cagr,
        "drawdown_improvement": request.candidate_performance.max_drawdown
        - request.incumbent_performance.max_drawdown,
        "calmar_change": request.candidate_performance.calmar
        - request.incumbent_performance.calmar,
    }
    if (
        AuditStatus.FAIL
        in (request.integrity, request.reproducibility, request.technical_replay)
        or request.blocking_findings
    ):
        return MandateChallengeResult(
            **common,
            decision=BenchmarkChallengeDecision.KEEP_BENCHMARK,
            risk_label=RiskLabel.WEAK,
            reason_codes=("REQUIRED_AUDIT_OR_BLOCKING_FINDING",),
        )
    if AuditStatus.INSUFFICIENT in (
        request.integrity,
        request.reproducibility,
        request.technical_replay,
    ):
        return MandateChallengeResult(
            **common,
            decision=BenchmarkChallengeDecision.INSUFFICIENT_EVIDENCE,
            risk_label=RiskLabel.MIXED,
            reason_codes=("INCOMPLETE_REQUIRED_AUDIT",),
        )
    if not candidate_passed:
        return MandateChallengeResult(
            **common,
            decision=BenchmarkChallengeDecision.KEEP_BENCHMARK,
            risk_label=RiskLabel.WEAK,
            reason_codes=("CANDIDATE_MANDATE_NOT_MET",),
        )
    if incumbent_passed:
        return MandateChallengeResult(
            **common,
            decision=BenchmarkChallengeDecision.INSUFFICIENT_EVIDENCE,
            risk_label=RiskLabel.MIXED,
            reason_codes=("STANDARD_DOMINANCE_CHALLENGE_REQUIRED",),
        )
    return MandateChallengeResult(
        **common,
        decision=BenchmarkChallengeDecision.RECOMMEND_HEALTH_CHECK,
        risk_label=RiskLabel.MIXED,
        reason_codes=(
            "CANDIDATE_MANDATE_MET",
            "INCUMBENT_MANDATE_VIOLATED",
            "RISK_RETURN_TRADEOFF_DISCLOSED",
        ),
    )
