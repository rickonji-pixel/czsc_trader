from dataclasses import FrozenInstanceError

import pytest


PROTOCOL = {
    "schema_version": 1,
    "standard_version": "opc-v1",
    "experiment_id": "0903_TEST",
    "research_objective": "改善震荡区间表现",
    "development_cutoff": "2026-09-02",
    "incumbent_id": "S001-v1",
    "incumbent_hash": "a" * 64,
    "decision_windows": ["full", "2026_ytd"],
    "target_windows": ["range"],
    "execution_policy_hash": "b" * 64,
    "tightened_margins": {"net_cagr_retention": 0.95},
    "shortlist_limit": 12,
    "target_requirements": [
        {"metric": "range_return", "direction": "maximize", "minimum_improvement": 0.01}
    ],
    "candidate_manifest": "candidate_manifest.json",
}

OBSERVATION = {
    "candidate_id": "S001-v1",
    "window_id": "full",
    "scenario_id": "standard",
    "measurement_tier": "FORMAL",
    "net_cagr": 0.1,
    "total_return": 0.6,
    "max_drawdown": -0.12,
    "calmar": 0.83,
    "calmar_status": "VALID",
    "profit_factor": 2.1,
    "profit_factor_status": "VALID",
    "closed_trades": 30,
    "turnover": 4.0,
    "cost_drag": 0.01,
    "objective_values": {"range_return": -0.1},
}


def test_protocol_and_observation_are_immutable_and_json_compatible():
    from strategy_evaluator import EvaluationProtocol, MetricObservation, MetricStatus

    protocol = EvaluationProtocol.from_dict(PROTOCOL)
    observation = MetricObservation.from_dict(OBSERVATION)

    assert protocol.standard_version == "opc-v1"
    assert observation.profit_factor_status is MetricStatus.VALID
    assert dict(observation.objective_values) == {"range_return": -0.1}
    assert EvaluationProtocol.from_dict(protocol.to_dict()) == protocol
    assert MetricObservation.from_dict(observation.to_dict()) == observation
    with pytest.raises(FrozenInstanceError):
        protocol.experiment_id = "changed"


def test_decision_and_status_values_are_closed():
    from strategy_evaluator import Decision, HealthStatus, MetricStatus

    assert {item.value for item in Decision} == {
        "RECOMMEND_FREEZE",
        "KEEP_INCUMBENT",
        "INSUFFICIENT_EVIDENCE",
    }
    assert {item.value for item in HealthStatus} == {"PASS", "FAIL", "INSUFFICIENT"}
    assert "LOW_SAMPLE" in {item.value for item in MetricStatus}


def test_models_reject_unknown_fields_and_mutable_nested_values():
    from strategy_evaluator import EvaluationProtocol, MetricObservation, ValidationError

    with pytest.raises(ValidationError, match="unknown fields"):
        EvaluationProtocol.from_dict({**PROTOCOL, "surprise": True})
    with pytest.raises(ValidationError, match="objective_values"):
        MetricObservation.from_dict({**OBSERVATION, "objective_values": [1, 2]})
