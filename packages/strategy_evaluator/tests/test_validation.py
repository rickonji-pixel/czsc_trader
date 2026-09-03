from copy import deepcopy

import pytest

from strategy_evaluator import (
    CandidateDescriptor,
    EvaluationProtocol,
    MetricObservation,
    TrialRecord,
    ValidationError,
    validate_protocol,
)

from test_models import OBSERVATION, PROTOCOL


def inputs(margins=None):
    raw = deepcopy(PROTOCOL)
    raw["tightened_margins"] = margins or {"net_cagr_retention": 0.95}
    protocol = EvaluationProtocol.from_dict(raw)
    candidates = (
        CandidateDescriptor("S001-v1", "a" * 64, "b" * 64, True, "h1"),
        CandidateDescriptor("c1", "c" * 64, "b" * 64, False, "h2"),
    )
    observations = tuple(
        MetricObservation.from_dict({**OBSERVATION, "candidate_id": cid, "window_id": window})
        for cid in ("S001-v1", "c1")
        for window in ("full", "2026_ytd")
    )
    trials = (
        TrialRecord("t0", "S001-v1", "a" * 64, "h1", "COMPLETED"),
        TrialRecord("t1", "c1", "c" * 64, "h2", "COMPLETED"),
    )
    return protocol, candidates, observations, trials


def test_protocol_can_tighten_but_cannot_loosen_defaults():
    validate_protocol(*inputs())
    with pytest.raises(ValidationError, match="cannot loosen") as error:
        validate_protocol(*inputs({"net_cagr_retention": 0.80}))
    assert error.value.code == "MARGIN_LOOSENED"


def test_protocol_accepts_opc_v2_standard():
    protocol, candidates, observations, trials = inputs()
    raw = protocol.to_dict()
    raw["standard_version"] = "opc-v2"
    validate_protocol(EvaluationProtocol.from_dict(raw), candidates, observations, trials)


def test_validation_requires_one_incumbent_and_complete_trial_ledger():
    protocol, candidates, observations, trials = inputs()
    no_incumbent = tuple(CandidateDescriptor(c.candidate_id, c.candidate_hash, c.execution_policy_hash, False, c.behavior_hash) for c in candidates)
    with pytest.raises(ValidationError, match="exactly one incumbent"):
        validate_protocol(protocol, no_incumbent, observations, trials)
    with pytest.raises(ValidationError, match="trial ledger"):
        validate_protocol(protocol, candidates, observations, trials[:-1])


def test_validation_rejects_execution_hash_mismatch_and_invalid_metrics():
    protocol, candidates, observations, trials = inputs()
    bad = CandidateDescriptor("c1", "c" * 64, "x" * 64, False, "h2")
    with pytest.raises(ValidationError, match="execution policy hash"):
        validate_protocol(protocol, (candidates[0], bad), observations, trials)
    invalid = MetricObservation.from_dict({**OBSERVATION, "candidate_id": "c1", "max_drawdown": 0.1})
    with pytest.raises(ValidationError, match="max_drawdown"):
        validate_protocol(protocol, candidates, (*observations[:2], invalid, *observations[3:]), trials)


def test_validation_rejects_duplicate_observations_and_trial_hash_mismatch():
    protocol, candidates, observations, trials = inputs()
    with pytest.raises(ValidationError, match="duplicate observation"):
        validate_protocol(protocol, candidates, observations + (observations[0],), trials)
    bad_trials = (trials[0], TrialRecord("t1", "c1", "f" * 64, "h2", "COMPLETED"))
    with pytest.raises(ValidationError, match="trial hash"):
        validate_protocol(protocol, candidates, observations, bad_trials)
