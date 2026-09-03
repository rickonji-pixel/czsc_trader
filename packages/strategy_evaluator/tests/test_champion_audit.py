import numpy as np


def complete_request(*, champion_edge: float = 0.0002, execution_invalid: bool = False):
    from strategy_evaluator import (
        AuditIdentity,
        CandidateDescriptor,
        CandidateProfile,
        ChampionAuditRequest,
        ExecutionEvidence,
        ExecutionOrder,
        FactorEvent,
        MetricObservation,
        MetricStatus,
        ParameterPoint,
        ReturnMatrixEvidence,
        StressScenarioResult,
        TrialRecord,
        hash_audit_data,
        hash_candidate_pool,
        hash_execution_evidence,
        hash_return_matrix,
        required_stress_scenarios,
    )

    dates = tuple(f"2025-{month:02d}-{day:02d}" for month in range(1, 6) for day in range(1, 7))
    base = np.tile([0.001, -0.0008, 0.0004], 10)
    search_ids = tuple(["R1102", "R0539", *[f"R{index:04d}" for index in range(10)]])
    search_values = np.column_stack(
        [base + champion_edge, base + 0.0001, *[base + index * 0.00001 for index in range(10)]]
    )
    search = ReturnMatrixEvidence(
        dates, search_ids, tuple(tuple(map(float, row)) for row in search_values), "",
    )
    search = ReturnMatrixEvidence(search.dates, search.candidate_ids, search.returns, hash_return_matrix(search))
    comparison_values = np.column_stack([base + champion_edge, base, base + 0.0001])
    comparison = ReturnMatrixEvidence(
        dates, ("R1102", "S001-v1", "R0539"),
        tuple(tuple(map(float, row)) for row in comparison_values), "",
    )
    comparison = ReturnMatrixEvidence(
        comparison.dates, comparison.candidate_ids, comparison.returns,
        hash_return_matrix(comparison),
    )
    candidates = tuple(
        CandidateDescriptor(
            candidate_id, f"{index + 1:064x}", "c" * 64,
            candidate_id == "S001-v1", f"behavior-{candidate_id}",
        )
        for index, candidate_id in enumerate(("S001-v1", *search_ids))
    )
    profiles = tuple(
        CandidateProfile(
            candidate_id, True, True,
            (("net_cagr", 0.5), ("max_drawdown", 0.5), ("calmar", 0.5),
             ("profit_factor", 0.5)), 1,
        )
        for candidate_id in search_ids
    )
    parameters = tuple(
        ParameterPoint(
            candidate_id, f"behavior-{candidate_id}",
            (("fast", 0.5 + index / 100), ("slow", 0.5 - index / 100)), True, 1,
            (("net_cagr", 0.5), ("max_drawdown", 0.5), ("calmar", 0.5),
             ("profit_factor", 0.5)), candidate_id == "S001-v1",
        )
        for index, candidate_id in enumerate(("R1102", "S001-v1", *search_ids[1:]))
    )
    trials = tuple(
        TrialRecord(f"t{index}", item.candidate_id, item.candidate_hash,
                    item.behavior_hash, "COMPLETED")
        for index, item in enumerate(candidates)
    )
    execution_date = "2025-01-03" if not execution_invalid else "2025-01-04"
    execution = ExecutionEvidence(
        dates, tuple([0.0, 1.0, *([1.0] * 28)]), tuple([0.0, 0.2, *([0.1] * 28)]),
        (FactorEvent("entry", dates[1], "Entry", 0.0, 1.0, 0.2),),
        (ExecutionOrder(dates[1], execution_date, "Buy", 10.0, 10.0, 0.05, "entry"),),
        "",
    )
    execution = ExecutionEvidence(
        execution.dates, execution.target_positions, execution.factor_scores,
        execution.events, execution.orders, hash_execution_evidence(execution),
    )

    def observation(candidate_id: str, scenario: str = "standard"):
        edge = champion_edge if candidate_id == "R1102" else 0.0
        return MetricObservation(
            candidate_id, "full", scenario,
            "FORMAL" if scenario == "standard" else "STRESS",
            0.1 + edge * 100, 0.5, -0.2 + edge * 10,
            0.5 + edge * 50, MetricStatus.VALID, 2.0 + edge * 100,
            MetricStatus.VALID, 20,
        )

    formal = (observation("S001-v1"), observation("R1102"))
    stress = tuple(
        StressScenarioResult(
            scenario.scenario_id, "c" * 64,
            (observation("S001-v1", scenario.scenario_id),
             observation("R1102", scenario.scenario_id)),
        )
        for scenario in required_stress_scenarios()
    )
    identity = AuditIdentity(
        "0903_EX06", hash_candidate_pool(candidates), "2026-09-02",
        hash_audit_data(search, comparison), "c" * 64, "opc-v3",
        "champion-audit-v1", 20260903,
    )
    return ChampionAuditRequest(
        identity, "R1102", "S001-v1", ("R0539",), search, comparison,
        parameters, execution, formal, formal, candidates, trials, profiles, stress,
        100, (21, 10, 42),
    )


def ranking():
    from strategy_evaluator import CandidateProfile, RankingResult

    profile = CandidateProfile(
        "R1102", True, True,
        (("net_cagr", 1.0), ("max_drawdown", 1.0), ("calmar", 1.0),
         ("profit_factor", 1.0)), 1, 1.0,
    )
    return RankingResult("S001-v1", (profile,), "R1102", ("R1102",))


def test_complete_opc_v3_audit_can_recommend_with_weak_statistics() -> None:
    from strategy_evaluator import (
        Decision, RiskLabel, audit_provisional_champion, finalize_evaluation,
    )

    audit = audit_provisional_champion(complete_request(champion_edge=-0.0002))
    result = finalize_evaluation(
        ranking(), None, "0903_EX06", audit=audit, standard_version="opc-v3",
    )

    assert audit.risk_label is RiskLabel.WEAK
    assert result.decision is Decision.RECOMMEND_FREEZE
    assert result.audit == audit


def test_missing_opc_v3_audit_is_insufficient() -> None:
    from strategy_evaluator import Decision, finalize_evaluation

    result = finalize_evaluation(ranking(), None, "0903_EX06", standard_version="opc-v3")

    assert result.decision is Decision.INSUFFICIENT_EVIDENCE


def test_engineering_failure_keeps_incumbent() -> None:
    from strategy_evaluator import Decision, audit_provisional_champion, finalize_evaluation

    audit = audit_provisional_champion(complete_request(execution_invalid=True))
    result = finalize_evaluation(
        ranking(), None, "0903_EX06", audit=audit, standard_version="opc-v3",
    )

    assert result.decision is Decision.KEEP_INCUMBENT


def test_content_hash_mismatch_is_insufficient() -> None:
    from dataclasses import replace

    from strategy_evaluator import AuditStatus, audit_provisional_champion

    request = complete_request()
    request = replace(request, search_returns=replace(request.search_returns, content_hash="0" * 64))

    assert audit_provisional_champion(request).status is AuditStatus.INSUFFICIENT
