from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from czsc_trader.search import (
    ConstraintSpec,
    FloatParameter,
    IntParameter,
    ObjectiveSpec,
    SearchEvaluation,
    SearchExecutionError,
    SearchIntegrationError,
    SearchSpec,
    SearchTrialRejected,
    run_search,
)


def _evaluation(params: dict[str, object]) -> SearchEvaluation:
    x = float(params["x"])
    candidate = f"candidate-{x}"
    digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
    return SearchEvaluation(
        objectives={"quality": -(x - 1.5) ** 2},
        constraints={"drawdown": abs(x) / 10},
        candidate_id=candidate,
        strategy_hash=digest,
        behavior_hash=digest,
    )


def _random_spec() -> SearchSpec:
    return SearchSpec(
        study_name="deterministic-random",
        method="random",
        parameters=(FloatParameter("x", -2.0, 3.0),),
        objectives=(ObjectiveSpec("quality", "maximize"),),
        constraints=(ConstraintSpec("drawdown", 0.2),),
        target_trials=8,
        seed=17,
    )


def test_search_is_reproducible_and_emits_complete_ledger(tmp_path: Path) -> None:
    first = run_search(_random_spec(), _evaluation)
    second = run_search(_random_spec(), _evaluation)

    assert first.status == "PASS"
    assert first.outcome == "CANDIDATES_AVAILABLE"
    assert first.failed_trials == 0
    assert first.feasible_trials > 0
    assert first.ledger["params"].tolist() == second.ledger["params"].tolist()
    assert first.ledger["objective.quality"].tolist() == second.ledger["objective.quality"].tolist()
    assert set(first.ledger["state"]) == {"COMPLETE"}
    assert first.ledger["strategy_hash"].str.len().eq(64).all()
    assert first.ledger["metadata"].map(json.loads).map(lambda value: isinstance(value, dict)).all()
    assert not list(tmp_path.iterdir())


def test_persistent_grid_resumes_and_rejects_contract_drift(tmp_path: Path) -> None:
    storage = tmp_path / "resume.sqlite3"
    base = dict(
        study_name="resumable-grid",
        method="grid",
        parameters=(IntParameter("x", 0, 3),),
        objectives=(
            ObjectiveSpec("quality", "maximize"),
            ObjectiveSpec("risk", "minimize"),
        ),
        constraints=(ConstraintSpec("drawdown", 1.5),),
        seed=3,
        storage_path=storage,
        grid={"x": [0, 1, 2, 3]},
    )

    def evaluator(params: dict[str, object]) -> SearchEvaluation:
        x = float(params["x"])
        return SearchEvaluation(
            {"quality": x, "risk": 3 - x},
            {"drawdown": x},
        )
    partial = run_search(SearchSpec(**base, target_trials=2), evaluator)
    resumed = run_search(SearchSpec(**base, target_trials=4), evaluator)
    unchanged = run_search(SearchSpec(**base, target_trials=4), evaluator)

    assert partial.created_trials == 2
    assert resumed.existing_trials == 2
    assert resumed.created_trials == 2
    assert sorted(json.loads(value)["x"] for value in resumed.ledger["params"]) == [0, 1, 2, 3]
    assert {"objective.quality", "objective.risk", "constraint_violation.drawdown"}.issubset(
        resumed.ledger.columns
    )
    assert resumed.feasible_trials == 2
    assert unchanged.outcome == "NO_NEW_TRIALS"
    assert unchanged.created_trials == 0

    with pytest.raises(SearchIntegrationError, match="different search contract"):
        run_search(
            SearchSpec(**{**base, "seed": 99, "target_trials": 4}),
            evaluator,
        )


def test_expected_rejection_is_audited_as_pruned() -> None:
    spec = SearchSpec(
        study_name="pruned-grid",
        method="grid",
        parameters=(IntParameter("x", 0, 2),),
        objectives=(ObjectiveSpec("quality", "maximize"),),
        target_trials=3,
        seed=1,
        grid={"x": [0, 1, 2]},
    )

    def evaluator(params: dict[str, object]) -> SearchEvaluation:
        if params["x"] == 1:
            raise SearchTrialRejected("INVALID_COMBINATION", "x=1 is structurally invalid")
        return SearchEvaluation({"quality": float(params["x"])})

    result = run_search(spec, evaluator)

    assert result.completed_trials == 2
    assert result.pruned_trials == 1
    rejected = result.ledger.loc[result.ledger["state"].eq("PRUNED")].iloc[0]
    assert rejected["rejection_code"] == "INVALID_COMBINATION"


def test_evaluator_failure_and_invalid_contract_never_report_success() -> None:
    spec = SearchSpec(
        study_name="strict-study",
        method="random",
        parameters=(FloatParameter("x", 0.0, 1.0),),
        objectives=(ObjectiveSpec("quality", "maximize"),),
        target_trials=1,
        seed=2,
    )
    with pytest.raises(SearchExecutionError, match="evaluator failure"):
        run_search(spec, lambda _: SearchEvaluation({"wrong_name": 1.0}))

    with pytest.raises(SearchIntegrationError, match="exceeds grid combinations"):
        run_search(
            SearchSpec(
                study_name="oversized-grid",
                method="grid",
                parameters=(IntParameter("x", 0, 1),),
                objectives=(ObjectiveSpec("quality", "maximize"),),
                target_trials=3,
                seed=1,
                grid={"x": [0, 1]},
            ),
            lambda _: SearchEvaluation({"quality": 1.0}),
        )
