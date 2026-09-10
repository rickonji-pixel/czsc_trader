from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .audit_models import AuditStatus, RiskLabel
from .bootstrap import BootstrapComparison, PerformanceMetrics
from .models import Record


class BenchmarkChallengeDecision(str, Enum):
    """Decision for a candidate challenging an explicit benchmark."""

    RECOMMEND_FREEZE = "RECOMMEND_FREEZE"
    KEEP_BENCHMARK = "KEEP_BENCHMARK"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True)
class BenchmarkChallengeRequest(Record):
    candidate_id: str
    candidate_hash: str
    benchmark_id: str
    integrity: AuditStatus
    reproducibility: AuditStatus
    technical_replay: AuditStatus
    monitoring_plan: AuditStatus
    candidate_performance: PerformanceMetrics
    benchmark_performance: PerformanceMetrics
    bootstrap: BootstrapComparison
    statistical_evidence: RiskLabel
    external_validation: RiskLabel
    execution_evidence: RiskLabel
    blocking_findings: tuple[str, ...] = ()


@dataclass(frozen=True)
class BenchmarkChallengeResult(Record):
    candidate_id: str
    candidate_hash: str
    benchmark_id: str
    decision: BenchmarkChallengeDecision
    risk_label: RiskLabel
    reason_codes: tuple[str, ...]


def assess_benchmark_challenge(
    request: BenchmarkChallengeRequest,
) -> BenchmarkChallengeResult:
    """Assess whether a candidate has earned freeze review against a benchmark.

    The benchmark is the explicit opponent. A favorable point estimate and a
    greater-than-even paired-bootstrap probability are required for CAGR,
    maximum drawdown and Calmar. Risk labels remain visible instead of being
    converted into hidden numeric thresholds.
    """
    if not all(
        value.strip()
        for value in (request.candidate_id, request.candidate_hash, request.benchmark_id)
    ):
        raise ValueError("candidate and benchmark identity must be nonblank")
    if (
        request.bootstrap.champion_id != request.candidate_id
        or request.bootstrap.comparator_id != request.benchmark_id
    ):
        raise ValueError("bootstrap identity differs from challenge identity")

    audits = (
        request.integrity,
        request.reproducibility,
        request.technical_replay,
        request.monitoring_plan,
    )
    labels = (
        request.statistical_evidence,
        request.external_validation,
        request.execution_evidence,
    )
    failure_reasons: list[str] = []
    if AuditStatus.FAIL in audits:
        failure_reasons.append("REQUIRED_AUDIT_FAILED")
    if request.blocking_findings:
        failure_reasons.append("BLOCKING_FINDINGS")
    if RiskLabel.WEAK in labels:
        failure_reasons.append("WEAK_SUPPORTING_EVIDENCE")
    if failure_reasons:
        return BenchmarkChallengeResult(
            request.candidate_id,
            request.candidate_hash,
            request.benchmark_id,
            BenchmarkChallengeDecision.KEEP_BENCHMARK,
            RiskLabel.WEAK,
            tuple(failure_reasons),
        )
    if AuditStatus.INSUFFICIENT in audits:
        return BenchmarkChallengeResult(
            request.candidate_id,
            request.candidate_hash,
            request.benchmark_id,
            BenchmarkChallengeDecision.INSUFFICIENT_EVIDENCE,
            RiskLabel.MIXED,
            ("INCOMPLETE_REQUIRED_AUDIT",),
        )

    point_dominance = (
        request.candidate_performance.cagr > request.benchmark_performance.cagr
        and request.candidate_performance.max_drawdown
        > request.benchmark_performance.max_drawdown
        and request.candidate_performance.calmar > request.benchmark_performance.calmar
    )
    if not point_dominance:
        return BenchmarkChallengeResult(
            request.candidate_id,
            request.candidate_hash,
            request.benchmark_id,
            BenchmarkChallengeDecision.KEEP_BENCHMARK,
            RiskLabel.WEAK,
            ("BENCHMARK_NOT_DOMINATED",),
        )

    bootstrap_probabilities = (
        request.bootstrap.cagr.probability_favorable,
        request.bootstrap.max_drawdown.probability_favorable,
        request.bootstrap.calmar.probability_favorable,
    )
    if not all(value > 0.5 for value in bootstrap_probabilities):
        return BenchmarkChallengeResult(
            request.candidate_id,
            request.candidate_hash,
            request.benchmark_id,
            BenchmarkChallengeDecision.KEEP_BENCHMARK,
            RiskLabel.WEAK,
            ("BOOTSTRAP_ADVANTAGE_NOT_MAJORITY",),
        )

    risk = (
        RiskLabel.FAVORABLE
        if all(label is RiskLabel.FAVORABLE for label in labels)
        else RiskLabel.MIXED
    )
    return BenchmarkChallengeResult(
        request.candidate_id,
        request.candidate_hash,
        request.benchmark_id,
        BenchmarkChallengeDecision.RECOMMEND_FREEZE,
        risk,
        (
            "REQUIRED_AUDITS_PASSED",
            "BENCHMARK_DOMINATED_ON_POINT_METRICS",
            "PAIRED_BOOTSTRAP_ADVANTAGE_MAJORITY",
            "NO_BLOCKING_FINDINGS",
        ),
    )
