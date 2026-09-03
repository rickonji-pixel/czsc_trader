from dataclasses import FrozenInstanceError

import pytest


IDENTITY = {
    "experiment_id": "0903_EX06",
    "pool_hash": "a" * 64,
    "data_cutoff": "2026-09-02",
    "data_hash": "b" * 64,
    "execution_policy_hash": "c" * 64,
    "standard_version": "opc-v3",
    "audit_protocol_version": "champion-audit-v1",
    "seed": 20260903,
}


def test_audit_identity_is_immutable_and_round_trips() -> None:
    from strategy_evaluator import AuditIdentity

    identity = AuditIdentity.from_dict(IDENTITY)

    assert identity.standard_version == "opc-v3"
    assert AuditIdentity.from_dict(identity.to_dict()) == identity
    with pytest.raises(FrozenInstanceError):
        identity.seed = 1


def test_return_matrix_round_trips_without_pandas() -> None:
    from strategy_evaluator import ReturnMatrixEvidence

    raw = {
        "dates": ["2026-09-01", "2026-09-02"],
        "candidate_ids": ["R1102", "R0539"],
        "returns": [[0.01, 0.02], [-0.01, 0.0]],
        "content_hash": "d" * 64,
    }

    evidence = ReturnMatrixEvidence.from_dict(raw)

    assert evidence.returns == ((0.01, 0.02), (-0.01, 0.0))
    assert ReturnMatrixEvidence.from_dict(evidence.to_dict()) == evidence


def test_audit_status_and_risk_label_are_closed() -> None:
    from strategy_evaluator import AuditStatus, RiskLabel

    assert {item.value for item in AuditStatus} == {"PASS", "FAIL", "INSUFFICIENT"}
    assert {item.value for item in RiskLabel} == {"FAVORABLE", "MIXED", "WEAK"}


def test_opc_v3_reuses_opc_v2_deterministic_margins() -> None:
    from strategy_evaluator import OPC_V2, OPC_V3

    assert OPC_V3.margins == OPC_V2.margins


def test_complete_audit_request_round_trips() -> None:
    from strategy_evaluator import (
        AuditIdentity,
        CandidateDescriptor,
        CandidateProfile,
        ChampionAuditRequest,
        ExecutionEvidence,
        MetricObservation,
        MetricStatus,
        ParameterPoint,
        ReturnMatrixEvidence,
        StressScenarioResult,
        TrialRecord,
    )

    matrix = ReturnMatrixEvidence(("2026-09-01",), ("R1102",), ((0.01,),), "d" * 64)
    observation = MetricObservation(
        "R1102", "full", "standard", "FORMAL", 0.2, 0.2, -0.1, 2.0,
        MetricStatus.VALID, 2.0, MetricStatus.VALID, 12,
    )
    request = ChampionAuditRequest(
        AuditIdentity.from_dict(IDENTITY), "R1102", "S001-v1", ("R0539",), matrix,
        matrix, (ParameterPoint("R1102", "e" * 64, (("w", 1.0),), True, 1,
                               (("calmar", 1.0),)),),
        ExecutionEvidence(("2026-09-01",), (1.0,), (0.5,), (), (), "f" * 64),
        (observation,), (observation,),
        (CandidateDescriptor("R1102", "1" * 64, "c" * 64),),
        (TrialRecord("t1", "R1102", "1" * 64, "e" * 64, "COMPLETED"),),
        (CandidateProfile("R1102", True, True, (("calmar", 1.0),), 1),),
        (StressScenarioResult("fee_x2", "c" * 64, (observation,)),),
    )

    assert ChampionAuditRequest.from_dict(request.to_dict()) == request
