from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from enum import Enum

import numpy as np

from .audit_models import AuditStatus, ChampionAuditRequest, ChampionAuditResult, RiskLabel
from .bootstrap import performance_metrics
from .champion_audit import audit_provisional_champion
from .models import MetricObservation, Record


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT = "INSUFFICIENT"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class MachineVerdict(str, Enum):
    ELIGIBLE_FOR_FREEZE_REVIEW = "ELIGIBLE_FOR_FREEZE_REVIEW"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True)
class MachineEvaluationPolicy(Record):
    policy_id: str
    policy_version: str
    allowed_risk_labels: tuple[RiskLabel, ...] = (RiskLabel.FAVORABLE, RiskLabel.MIXED)
    minimum_bootstrap_probability: float = 0.5
    required_external_replays: int = 0
    external_minimum_cagr: float = 0.0
    external_max_drawdown_floor: float = -1.0
    stress_minimum_cagr: float = 0.0
    stress_max_drawdown_floor: float = -1.0
    stress_minimum_calmar: float = 0.0

    def __post_init__(self) -> None:
        if not self.policy_id.strip() or not self.policy_version.strip():
            raise ValueError("machine evaluation policy identity must be nonblank")
        if not self.allowed_risk_labels:
            raise ValueError("machine evaluation policy requires allowed risk labels")
        if not 0.0 <= self.minimum_bootstrap_probability < 1.0:
            raise ValueError("minimum bootstrap probability must be in [0, 1)")
        if self.required_external_replays < 0:
            raise ValueError("required external replays must be nonnegative")
        if not -1.0 <= self.external_max_drawdown_floor <= 0.0:
            raise ValueError("external maximum drawdown floor must be in [-1, 0]")
        if not -1.0 <= self.stress_max_drawdown_floor <= 0.0:
            raise ValueError("stress maximum drawdown floor must be in [-1, 0]")


@dataclass(frozen=True)
class ExternalReplayEvidence(Record):
    replay_id: str
    symbol: str
    candidate_id: str
    candidate_hash: str
    dates: tuple[str, ...]
    returns: tuple[float, ...]


@dataclass(frozen=True)
class MachineEvaluationCase(Record):
    report_id: str
    candidate_hash: str
    policy: MachineEvaluationPolicy
    audit_request: ChampionAuditRequest
    external_replays: tuple[ExternalReplayEvidence, ...] = ()


@dataclass(frozen=True)
class MachineCheckResult(Record):
    check_id: str
    status: CheckStatus
    reason_codes: tuple[str, ...] = ()
    metrics: tuple[tuple[str, float | int | str], ...] = ()


@dataclass(frozen=True)
class MachineEvaluationReport(Record):
    schema_version: int
    report_id: str
    candidate_id: str
    candidate_hash: str
    policy_id: str
    policy_version: str
    evaluator_version: str
    risk_label: RiskLabel | None
    checks: tuple[MachineCheckResult, ...]
    machine_verdict: MachineVerdict
    reason_codes: tuple[str, ...]
    evidence_hash: str
    audit_result: ChampionAuditResult
    report_hash: str


_SHA256 = re.compile(r"[0-9a-f]{64}")


def _canonical_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _check(
    check_id: str,
    status: CheckStatus,
    *reason_codes: str,
    metrics: dict[str, float | int | str] | None = None,
) -> MachineCheckResult:
    return MachineCheckResult(
        check_id,
        status,
        tuple(reason_codes),
        tuple(sorted((metrics or {}).items())),
    )


def _formal_observation(
    request: ChampionAuditRequest, candidate_id: str
) -> MetricObservation | None:
    matches = [
        item
        for item in request.formal_observations
        if item.candidate_id == candidate_id and item.window_id == "full"
    ]
    return matches[0] if len(matches) == 1 else None


def _evidence_integrity(case: MachineEvaluationCase) -> MachineCheckResult:
    request = case.audit_request
    candidates = [item for item in request.candidates if item.candidate_id == request.champion_id]
    if len(candidates) != 1:
        return _check("evidence_integrity", CheckStatus.FAIL, "CANDIDATE_IDENTITY_NOT_UNIQUE")
    if not _SHA256.fullmatch(case.candidate_hash):
        return _check("evidence_integrity", CheckStatus.FAIL, "INVALID_CANDIDATE_HASH")
    if candidates[0].candidate_hash != case.candidate_hash:
        return _check("evidence_integrity", CheckStatus.FAIL, "CANDIDATE_HASH_MISMATCH")
    if not case.report_id.strip() or not case.policy.policy_id.strip() or not case.policy.policy_version.strip():
        return _check("evidence_integrity", CheckStatus.FAIL, "MISSING_REPORT_OR_POLICY_IDENTITY")
    return _check("evidence_integrity", CheckStatus.PASS)


def _candidate_screening(request: ChampionAuditRequest) -> MachineCheckResult:
    profiles = [item for item in request.profiles if item.candidate_id == request.champion_id]
    if len(profiles) != 1:
        return _check("candidate_screening", CheckStatus.INSUFFICIENT, "MISSING_CANDIDATE_PROFILE")
    if not profiles[0].eligible:
        return _check("candidate_screening", CheckStatus.FAIL, "CANDIDATE_NOT_ELIGIBLE")
    return _check(
        "candidate_screening",
        CheckStatus.PASS,
        metrics={"pareto_layer": profiles[0].pareto_layer or 0},
    )


def _benchmark_challenge(case: MachineEvaluationCase, audit) -> MachineCheckResult:
    request = case.audit_request
    candidate = _formal_observation(request, request.champion_id)
    benchmark = _formal_observation(request, request.incumbent_id)
    if candidate is None or benchmark is None:
        return _check("benchmark_challenge", CheckStatus.INSUFFICIENT, "MISSING_FULL_WINDOW_METRICS")
    point_dominance = (
        candidate.net_cagr > benchmark.net_cagr
        and candidate.max_drawdown > benchmark.max_drawdown
        and candidate.calmar is not None
        and benchmark.calmar is not None
        and candidate.calmar > benchmark.calmar
    )
    primary = next(
        (
            item
            for item in audit.bootstrap
            if item.comparator_id == request.incumbent_id and item.mean_block_length == 21
        ),
        None,
    )
    if primary is None:
        return _check("benchmark_challenge", CheckStatus.INSUFFICIENT, "MISSING_PRIMARY_BOOTSTRAP")
    probabilities = (
        primary.cagr.probability_favorable,
        primary.max_drawdown.probability_favorable,
        primary.calmar.probability_favorable,
    )
    metrics = {
        "cagr_probability": probabilities[0],
        "drawdown_probability": probabilities[1],
        "calmar_probability": probabilities[2],
    }
    if not point_dominance:
        return _check("benchmark_challenge", CheckStatus.FAIL, "BENCHMARK_NOT_DOMINATED", metrics=metrics)
    if not all(value > case.policy.minimum_bootstrap_probability for value in probabilities):
        return _check(
            "benchmark_challenge",
            CheckStatus.FAIL,
            "BOOTSTRAP_ADVANTAGE_BELOW_POLICY",
            metrics=metrics,
        )
    return _check("benchmark_challenge", CheckStatus.PASS, metrics=metrics)


def _technical_consistency(audit) -> MachineCheckResult:
    required = {"execution", "reproducibility", "trial_ledger"}
    findings = [item for item in audit.findings if item.audit_id in required]
    if {item.audit_id for item in findings} != required:
        return _check("technical_consistency", CheckStatus.INSUFFICIENT, "MISSING_TECHNICAL_AUDIT")
    reasons = tuple(code for item in findings for code in item.reason_codes)
    if any(item.status is AuditStatus.FAIL for item in findings):
        return _check("technical_consistency", CheckStatus.FAIL, *reasons)
    if any(item.status is AuditStatus.INSUFFICIENT for item in findings):
        return _check("technical_consistency", CheckStatus.INSUFFICIENT, *reasons)
    return _check("technical_consistency", CheckStatus.PASS)


def _statistical_robustness(case: MachineEvaluationCase, audit) -> MachineCheckResult:
    if audit.search_bias is None or audit.dsr is None or not audit.bootstrap:
        return _check("statistical_robustness", CheckStatus.INSUFFICIENT, "MISSING_STATISTICAL_RESULT")
    metrics = {
        "pbo": float(audit.search_bias.pbo),
        "dsr_effective_probability": float(audit.dsr.effective.probability),
    }
    if audit.risk_label not in case.policy.allowed_risk_labels:
        return _check("statistical_robustness", CheckStatus.FAIL, "RISK_LABEL_REJECTED_BY_POLICY", metrics=metrics)
    return _check("statistical_robustness", CheckStatus.PASS, metrics=metrics)


def _parameter_robustness(audit) -> MachineCheckResult:
    if audit.neighborhood is None:
        return _check("parameter_robustness", CheckStatus.INSUFFICIENT, "MISSING_PARAMETER_AUDIT")
    status = CheckStatus(audit.neighborhood.status.value)
    return _check(
        "parameter_robustness",
        status,
        *audit.neighborhood.reason_codes,
        metrics={"valid_neighbor_count": audit.neighborhood.valid_neighbor_count},
    )


def _cost_stress(case: MachineEvaluationCase, audit) -> MachineCheckResult:
    if audit.stress is None:
        return _check("cost_stress", CheckStatus.INSUFFICIENT, "MISSING_COST_STRESS")
    if audit.stress.status is not AuditStatus.PASS:
        return _check(
            "cost_stress",
            CheckStatus(audit.stress.status.value),
            *audit.stress.reason_codes,
        )
    observations = [
        item
        for scenario in case.audit_request.stress_results
        for item in scenario.observations
        if item.candidate_id == case.audit_request.champion_id and item.window_id == "full"
    ]
    if not observations:
        return _check("cost_stress", CheckStatus.INSUFFICIENT, "MISSING_CANDIDATE_STRESS_METRICS")
    failures: list[str] = []
    metrics: dict[str, float | int | str] = {"scenarios": len(observations)}
    for item in observations:
        prefix = item.scenario_id
        metrics[f"{prefix}.cagr"] = item.net_cagr
        metrics[f"{prefix}.max_drawdown"] = item.max_drawdown
        if item.calmar is not None:
            metrics[f"{prefix}.calmar"] = item.calmar
        if item.net_cagr < case.policy.stress_minimum_cagr:
            failures.append("STRESS_CAGR_BELOW_POLICY")
        if item.max_drawdown < case.policy.stress_max_drawdown_floor:
            failures.append("STRESS_DRAWDOWN_BELOW_POLICY")
        if item.calmar is None or item.calmar < case.policy.stress_minimum_calmar:
            failures.append("STRESS_CALMAR_BELOW_POLICY")
    return _check(
        "cost_stress",
        CheckStatus.FAIL if failures else CheckStatus.PASS,
        *tuple(dict.fromkeys(failures)),
        metrics=metrics,
    )


def _external_reproduction(case: MachineEvaluationCase) -> MachineCheckResult:
    required = case.policy.required_external_replays
    if len(case.external_replays) < required:
        return _check(
            "external_reproduction",
            CheckStatus.INSUFFICIENT,
            "MISSING_REQUIRED_EXTERNAL_REPLAY",
            metrics={"required": required, "received": len(case.external_replays)},
        )
    if not case.external_replays:
        return _check("external_reproduction", CheckStatus.NOT_APPLICABLE)
    failures: list[str] = []
    metrics: dict[str, float | int | str] = {"replays": len(case.external_replays)}
    for replay in case.external_replays:
        if replay.candidate_id != case.audit_request.champion_id or replay.candidate_hash != case.candidate_hash:
            failures.append("EXTERNAL_REPLAY_IDENTITY_MISMATCH")
            continue
        try:
            parsed_dates = tuple(date.fromisoformat(value) for value in replay.dates)
        except ValueError:
            failures.append("INVALID_EXTERNAL_REPLAY_INDEX")
            continue
        if (
            len(replay.dates) != len(replay.returns)
            or len(set(parsed_dates)) != len(parsed_dates)
            or tuple(sorted(parsed_dates)) != parsed_dates
        ):
            failures.append("INVALID_EXTERNAL_REPLAY_INDEX")
            continue
        try:
            result = performance_metrics(np.asarray(replay.returns, dtype=float))
        except ValueError:
            failures.append("INVALID_EXTERNAL_REPLAY_RETURNS")
            continue
        metrics[f"{replay.replay_id}.cagr"] = result.cagr
        metrics[f"{replay.replay_id}.max_drawdown"] = result.max_drawdown
        if result.cagr < case.policy.external_minimum_cagr:
            failures.append("EXTERNAL_REPLAY_CAGR_BELOW_POLICY")
        if result.max_drawdown < case.policy.external_max_drawdown_floor:
            failures.append("EXTERNAL_REPLAY_DRAWDOWN_BELOW_POLICY")
    return _check(
        "external_reproduction",
        CheckStatus.FAIL if failures else CheckStatus.PASS,
        *tuple(dict.fromkeys(failures)),
        metrics=metrics,
    )


def evaluate_machine_eligibility(
    case: MachineEvaluationCase,
    *,
    evaluator_version: str = "machine-evaluation-v1",
) -> MachineEvaluationReport:
    """Calculate machine-verifiable freeze-review eligibility from raw SE evidence."""
    integrity = _evidence_integrity(case)
    audit = audit_provisional_champion(case.audit_request)
    checks = (
        integrity,
        _candidate_screening(case.audit_request),
        _benchmark_challenge(case, audit),
        _statistical_robustness(case, audit),
        _parameter_robustness(audit),
        _cost_stress(case, audit),
        _external_reproduction(case),
        _technical_consistency(audit),
    )
    if any(item.status is CheckStatus.FAIL for item in checks):
        verdict = MachineVerdict.NOT_ELIGIBLE
    elif any(item.status is CheckStatus.INSUFFICIENT for item in checks):
        verdict = MachineVerdict.INSUFFICIENT_EVIDENCE
    else:
        verdict = MachineVerdict.ELIGIBLE_FOR_FREEZE_REVIEW
    reasons = tuple(dict.fromkeys(code for item in checks for code in item.reason_codes))
    evidence_hash = _canonical_hash(case.to_dict())
    payload = {
        "schema_version": 2,
        "report_id": case.report_id,
        "candidate_id": case.audit_request.champion_id,
        "candidate_hash": case.candidate_hash,
        "policy_id": case.policy.policy_id,
        "policy_version": case.policy.policy_version,
        "evaluator_version": evaluator_version,
        "risk_label": None if audit.risk_label is None else audit.risk_label.value,
        "checks": [item.to_dict() for item in checks],
        "machine_verdict": verdict.value,
        "reason_codes": list(reasons),
        "evidence_hash": evidence_hash,
        "audit_result": audit.to_dict(),
    }
    return MachineEvaluationReport(
        2,
        case.report_id,
        case.audit_request.champion_id,
        case.candidate_hash,
        case.policy.policy_id,
        case.policy.policy_version,
        evaluator_version,
        audit.risk_label,
        checks,
        verdict,
        reasons,
        evidence_hash,
        audit,
        _canonical_hash(payload),
    )
