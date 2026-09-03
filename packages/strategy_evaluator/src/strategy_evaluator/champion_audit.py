from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import numpy as np

from .audit_models import (
    AuditFinding,
    AuditStatus,
    ChampionAuditRequest,
    ChampionAuditResult,
    ExecutionEvidence,
    ReturnMatrixEvidence,
    RiskLabel,
)
from .bootstrap import audit_pairwise_bootstrap
from .engineering_audit import (
    audit_execution,
    audit_reproducibility,
    audit_stress_results,
    audit_trial_ledger,
)
from .neighborhood import audit_parameter_neighborhood
from .search_bias import annualized_sharpe, calculate_dsr_bundle, cscv_pbo, effective_trial_count


def _canonical_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hash_return_matrix(evidence: ReturnMatrixEvidence) -> str:
    return _canonical_hash({
        "dates": list(evidence.dates),
        "candidate_ids": list(evidence.candidate_ids),
        "returns": [list(row) for row in evidence.returns],
    })


def hash_execution_evidence(evidence: ExecutionEvidence) -> str:
    return _canonical_hash({
        "dates": list(evidence.dates),
        "target_positions": list(evidence.target_positions),
        "factor_scores": list(evidence.factor_scores),
        "events": [item.to_dict() for item in evidence.events],
        "orders": [item.to_dict() for item in evidence.orders],
        "window_count": evidence.window_count,
    })


def hash_candidate_pool(candidates: tuple[Any, ...]) -> str:
    return _canonical_hash([
        item.to_dict() for item in sorted(candidates, key=lambda value: value.candidate_id)
    ])


def hash_audit_data(search: ReturnMatrixEvidence, comparison: ReturnMatrixEvidence) -> str:
    return _canonical_hash({
        "search_returns": search.content_hash,
        "comparison_returns": comparison.content_hash,
    })


def _incomplete(request: ChampionAuditRequest, *reasons: str) -> ChampionAuditResult:
    finding = AuditFinding("identity", AuditStatus.INSUFFICIENT, tuple(reasons))
    return ChampionAuditResult(
        request.identity, request.champion_id, AuditStatus.INSUFFICIENT, None,
        (finding,), tuple(reasons),
    )


def _validate_identity(request: ChampionAuditRequest) -> tuple[str, ...]:
    identity = request.identity
    reasons: list[str] = []
    if identity.standard_version != "opc-v3":
        reasons.append("AUDIT_REQUIRES_OPC_V3")
    if identity.audit_protocol_version != "champion-audit-v1":
        reasons.append("UNSUPPORTED_AUDIT_PROTOCOL")
    sha = re.compile(r"[0-9a-f]{64}")
    if any(not sha.fullmatch(value) for value in (
        identity.pool_hash, identity.data_hash, identity.execution_policy_hash,
        request.search_returns.content_hash, request.comparison_returns.content_hash,
        request.execution.content_hash,
    )):
        reasons.append("INVALID_AUDIT_HASH")
    if request.search_returns.content_hash != hash_return_matrix(request.search_returns):
        reasons.append("SEARCH_RETURN_HASH_MISMATCH")
    if request.comparison_returns.content_hash != hash_return_matrix(request.comparison_returns):
        reasons.append("COMPARISON_RETURN_HASH_MISMATCH")
    if request.execution.content_hash != hash_execution_evidence(request.execution):
        reasons.append("EXECUTION_EVIDENCE_HASH_MISMATCH")
    if identity.pool_hash != hash_candidate_pool(request.candidates):
        reasons.append("CANDIDATE_POOL_HASH_MISMATCH")
    if identity.data_hash != hash_audit_data(request.search_returns, request.comparison_returns):
        reasons.append("AUDIT_DATA_HASH_MISMATCH")
    if request.champion_id == request.incumbent_id:
        reasons.append("CHAMPION_EQUALS_INCUMBENT")
    if request.incumbent_id in request.search_returns.candidate_ids:
        reasons.append("INCUMBENT_IN_SEARCH_POOL")
    if request.champion_id not in request.search_returns.candidate_ids:
        reasons.append("CHAMPION_MISSING_FROM_SEARCH_POOL")
    if request.search_returns.dates != request.comparison_returns.dates:
        reasons.append("RETURN_DATE_MISMATCH")
    return tuple(reasons)


def _direction(value: float) -> str:
    if value > 0.5:
        return "FAVORABLE"
    if value < 0.5:
        return "WEAK"
    return "MIXED"


def _risk_label(flags: dict[str, str]) -> RiskLabel:
    values = tuple(flags.values())
    favorable = values.count("FAVORABLE")
    weak = values.count("WEAK")
    if weak >= 2:
        return RiskLabel.WEAK
    if weak == 0 and favorable >= 3:
        return RiskLabel.FAVORABLE
    return RiskLabel.MIXED


def audit_provisional_champion(request: ChampionAuditRequest) -> ChampionAuditResult:
    identity_reasons = _validate_identity(request)
    if identity_reasons:
        return _incomplete(request, *identity_reasons)

    execution = audit_execution(request.execution)
    reproducibility = audit_reproducibility(
        request.formal_observations, request.repeated_observations,
    )
    ledger = audit_trial_ledger(
        request.champion_id, request.candidates, request.trials, request.profiles,
    )
    stress = audit_stress_results(
        request.champion_id, request.incumbent_id,
        request.identity.execution_policy_hash, request.formal_observations,
        request.stress_results,
    )
    findings = [execution, reproducibility, ledger]
    findings.append(AuditFinding("stress", stress.status, stress.reason_codes))

    if any(item.status is AuditStatus.FAIL for item in findings):
        reasons = tuple(code for item in findings for code in item.reason_codes)
        return ChampionAuditResult(
            request.identity, request.champion_id, AuditStatus.FAIL, None,
            tuple(findings), reasons, stress=stress,
        )
    if any(item.status is AuditStatus.INSUFFICIENT for item in findings):
        reasons = tuple(code for item in findings for code in item.reason_codes)
        return ChampionAuditResult(
            request.identity, request.champion_id, AuditStatus.INSUFFICIENT, None,
            tuple(findings), reasons, stress=stress,
        )

    try:
        search_bias = cscv_pbo(request.search_returns, 10)
        search_matrix = np.asarray(request.search_returns.returns, dtype=float)
        trial_sharpes = np.asarray([
            annualized_sharpe(search_matrix[:, index])
            for index in range(search_matrix.shape[1])
        ])
        effective_count = effective_trial_count(search_matrix)
        champion_index = request.search_returns.candidate_ids.index(request.champion_id)
        raw_count = sum(trial.candidate_id != request.incumbent_id for trial in request.trials)
        dsr = calculate_dsr_bundle(
            search_matrix[:, champion_index], trial_sharpes,
            raw_count=raw_count, effective_count=effective_count,
        )
        bootstrap = audit_pairwise_bootstrap(
            request.comparison_returns, request.champion_id, request.incumbent_id,
            request.pareto_peer_ids, repetitions=request.bootstrap_repetitions,
            block_lengths=request.bootstrap_block_lengths, seed=request.identity.seed,
        )
        neighborhood = audit_parameter_neighborhood(request.champion_id, request.parameters)
    except (ValueError, IndexError) as exc:
        finding = AuditFinding(
            "statistical", AuditStatus.INSUFFICIENT,
            ("STATISTICAL_AUDIT_FAILED",), (("error", str(exc)),),
        )
        return ChampionAuditResult(
            request.identity, request.champion_id, AuditStatus.INSUFFICIENT, None,
            (*findings, finding), ("STATISTICAL_AUDIT_FAILED",), stress=stress,
        )

    neighborhood_finding = AuditFinding(
        "neighborhood", neighborhood.status, neighborhood.reason_codes,
        (("valid_neighbor_count", neighborhood.valid_neighbor_count),),
    )
    findings.append(neighborhood_finding)
    if neighborhood.status is AuditStatus.INSUFFICIENT:
        return ChampionAuditResult(
            request.identity, request.champion_id, AuditStatus.INSUFFICIENT, None,
            tuple(findings), neighborhood.reason_codes, search_bias, dsr, bootstrap,
            neighborhood, stress,
        )

    primary = next(
        item for item in bootstrap
        if item.comparator_id == request.incumbent_id and item.mean_block_length == 21
    )
    bootstrap_probabilities = (
        primary.cagr.probability_favorable,
        primary.max_drawdown.probability_favorable,
        primary.calmar.probability_favorable,
    )
    if all(value > 0.5 for value in bootstrap_probabilities):
        bootstrap_flag = "FAVORABLE"
    elif all(value < 0.5 for value in bootstrap_probabilities):
        bootstrap_flag = "WEAK"
    else:
        bootstrap_flag = "MIXED"
    dsr_probabilities = (dsr.raw.probability, dsr.effective.probability)
    if all(value > 0.5 for value in dsr_probabilities):
        dsr_flag = "FAVORABLE"
    elif all(value < 0.5 for value in dsr_probabilities):
        dsr_flag = "WEAK"
    else:
        dsr_flag = "MIXED"
    neighborhood_flag = (
        "FAVORABLE" if all(item.median >= -1.0 for item in neighborhood.metrics)
        else "WEAK"
    )
    flags = {
        "pbo": _direction(1.0 - search_bias.pbo),
        "dsr": dsr_flag,
        "bootstrap": bootstrap_flag,
        "neighborhood": neighborhood_flag,
    }
    return ChampionAuditResult(
        request.identity, request.champion_id, AuditStatus.PASS, _risk_label(flags),
        tuple(findings), (), search_bias, dsr, bootstrap, neighborhood, stress,
        tuple(sorted(flags.items())),
    )
