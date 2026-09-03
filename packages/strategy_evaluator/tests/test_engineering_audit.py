from dataclasses import replace


def execution_evidence(execution_date: str = "2026-01-07"):
    from strategy_evaluator import ExecutionEvidence, ExecutionOrder, FactorEvent

    dates = ("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08")
    return ExecutionEvidence(
        dates, (0.0, 1.0, 1.0, 0.0), (0.0, 0.2, 0.1, -0.1),
        (
            FactorEvent("entry", "2026-01-06", "Entry", 0.0, 1.0, 0.2),
            FactorEvent("exit", "2026-01-08", "Exit", 1.0, 0.0, -0.1),
        ),
        (ExecutionOrder("2026-01-06", execution_date, "Buy", 10.0, 10.0, 0.05, "entry"),),
        "a" * 64,
    )


def observations(scenario: str = "standard"):
    from strategy_evaluator import MetricObservation, MetricStatus

    return tuple(
        MetricObservation(
            candidate, "full", scenario, "FORMAL" if scenario == "standard" else "STRESS",
            cagr, cagr, drawdown, cagr / abs(drawdown), MetricStatus.VALID,
            profit_factor, MetricStatus.VALID, 12,
        )
        for candidate, cagr, drawdown, profit_factor in (
            ("S001-v1", 0.10, -0.20, 2.0), ("R1102", 0.12, -0.18, 2.2)
        )
    )


def test_execution_audit_rejects_non_next_day_fill() -> None:
    from strategy_evaluator import AuditStatus, audit_execution

    assert audit_execution(execution_evidence()).status is AuditStatus.PASS
    assert audit_execution(execution_evidence("2026-01-08")).status is AuditStatus.FAIL


def test_reproducibility_checks_values_status_and_windows() -> None:
    from strategy_evaluator import AuditStatus, audit_reproducibility

    formal = observations()
    assert audit_reproducibility(formal, formal).status is AuditStatus.PASS
    changed = replace(formal[0], net_cagr=formal[0].net_cagr + 1e-6)
    assert audit_reproducibility(formal, (changed, formal[1])).status is AuditStatus.FAIL
    assert audit_reproducibility(formal, formal[:1]).status is AuditStatus.INSUFFICIENT


def test_reproducibility_treats_objective_values_as_an_order_independent_mapping() -> None:
    from strategy_evaluator import AuditStatus, audit_reproducibility

    formal = observations()
    expected = replace(
        formal[0], objective_values=(("net_cagr", 0.10), ("total_return", 0.10)),
    )
    repeated = replace(
        formal[0], objective_values=(("total_return", 0.10), ("net_cagr", 0.10)),
    )

    assert audit_reproducibility((expected,), (repeated,)).status is AuditStatus.PASS


def test_trial_ledger_checks_hashes_coverage_and_champion_eligibility() -> None:
    from strategy_evaluator import (
        AuditStatus, CandidateDescriptor, CandidateProfile, TrialRecord, audit_trial_ledger,
    )

    candidates = (
        CandidateDescriptor("S001-v1", "a" * 64, "x" * 64, True, "ba"),
        CandidateDescriptor("R1102", "b" * 64, "x" * 64, False, "bb"),
    )
    trials = (
        TrialRecord("t0", "S001-v1", "a" * 64, "ba", "COMPLETED"),
        TrialRecord("t1", "R1102", "b" * 64, "bb", "COMPLETED"),
    )
    profiles = (CandidateProfile("R1102", True, True, (("calmar", 1.0),), 1),)

    assert audit_trial_ledger("R1102", candidates, trials, profiles).status is AuditStatus.PASS
    assert audit_trial_ledger("R1102", candidates, trials[:1], profiles).status is AuditStatus.INSUFFICIENT
    bad = replace(trials[1], strategy_hash="f" * 64)
    assert audit_trial_ledger("R1102", candidates, (trials[0], bad), profiles).status is AuditStatus.FAIL


def test_stress_requires_fee_and_all_slippage_scenarios() -> None:
    from strategy_evaluator import (
        AuditStatus, StressScenarioResult, audit_stress_results, required_stress_scenarios,
    )

    scenarios = required_stress_scenarios()
    assert {row.scenario_id for row in scenarios} == {
        "fee_x2", "slippage_15bp", "slippage_30bp", "slippage_50bp"
    }
    results = tuple(
        StressScenarioResult(
            scenario.scenario_id, "x" * 64,
            tuple(replace(row, scenario_id=scenario.scenario_id, measurement_tier="STRESS") for row in observations()),
        )
        for scenario in scenarios
    )

    assert audit_stress_results(
        "R1102", "S001-v1", "x" * 64, observations(), results
    ).status is AuditStatus.PASS
    assert audit_stress_results(
        "R1102", "S001-v1", "x" * 64, observations(), results[:-1]
    ).status is AuditStatus.INSUFFICIENT
