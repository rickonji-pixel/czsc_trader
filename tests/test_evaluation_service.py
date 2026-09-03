import json
from pathlib import Path
import shutil
from types import SimpleNamespace

from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.evaluation_service import accept_evaluation, evaluate_experiment
from strategy_evaluator import MetricObservation, MetricStatus


def write_bundle(root):
    (root / "src" / "czsc_trader").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\nversion='0.1'\n", encoding="utf-8")
    experiment = root / "experiments" / "0903_TEST"
    experiment.mkdir(parents=True)
    protocol = {
        "schema_version": 1, "standard_version": "opc-v1", "experiment_id": "0903_TEST", "research_objective": "improve range",
        "development_cutoff": "2026-09-02", "incumbent_id": "S001-v1", "incumbent_hash": "a" * 64,
        "decision_windows": ["full"], "target_windows": ["full"], "execution_policy_hash": "b" * 64,
        "tightened_margins": {}, "shortlist_limit": 12,
        "target_requirements": [{"metric": "full_return", "direction": "maximize", "minimum_improvement": 0.01}],
        "candidate_manifest": "candidate_manifest.json",
    }
    manifest = {
        "schema_version": 1, "symbol": "588080.SH", "asset_type": "etf", "fee_rate": 0.0005, "init_cash": 100000,
        "windows": {"full": {"start": "2021-01-04", "end": "2026-09-02"}},
        "candidates": [
            {"candidate_id": "S001-v1", "strategy_hash": "a" * 64, "execution_policy_hash": "b" * 64, "behavior_hash": "h0", "is_incumbent": True, "strategy_payload": {"rule": {"enter": 1}}},
            {"candidate_id": "c1", "strategy_id": "S001", "strategy_name": "综合基线策略", "strategy_hash": "c" * 64, "execution_policy_hash": "b" * 64, "behavior_hash": "h1", "family": "range", "parameter_group": "weights", "is_incumbent": False, "strategy_payload": {"rule": {"enter": 2}}},
            {"candidate_id": "c2", "strategy_id": "S001", "strategy_name": "综合基线策略", "strategy_hash": "d" * 64, "execution_policy_hash": "b" * 64, "behavior_hash": "h2", "family": "range", "parameter_group": "weights", "parameter_distance": 0.1, "is_incumbent": False, "strategy_payload": {"rule": {"enter": 3}}},
        ],
        "trials": [
            {"trial_id": "t0", "candidate_id": "S001-v1", "strategy_hash": "a" * 64, "behavior_hash": "h0", "status": "COMPLETED"},
            {"trial_id": "t1", "candidate_id": "c1", "strategy_hash": "c" * 64, "behavior_hash": "h1", "status": "COMPLETED"},
            {"trial_id": "t2", "candidate_id": "c2", "strategy_hash": "d" * 64, "behavior_hash": "h2", "status": "COMPLETED"},
        ],
    }
    (experiment / "evaluation_protocol.json").write_text(json.dumps(protocol), encoding="utf-8")
    (experiment / "candidate_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return experiment


def fake_runner(context, protocol, candidates, candidate_ids, tier, scenarios=("standard",)):
    rows = []
    for candidate_id in candidate_ids:
        better = candidate_id == "c1"
        for scenario in scenarios:
            rows.append(MetricObservation(candidate_id, "full", scenario, tier, 0.11 if better else 0.10, 0.55 if better else 0.50, -0.10, 1.1 if better else 1.0, MetricStatus.VALID, 2.1 if better else 2.0, MetricStatus.VALID, 20, 2.0, 0.01, (("full_return", 0.55 if better else 0.50),)))
    return tuple(rows)


def test_evaluate_experiment_writes_complete_atomic_result(tmp_path):
    experiment = write_bundle(tmp_path)
    context = RepositoryContext.discover(tmp_path, explicit_root=tmp_path)
    result = evaluate_experiment(context, "0903_TEST", runner=fake_runner)
    assert result.result["decision"] == "RECOMMEND_FREEZE"
    artifacts = experiment / "artifacts"
    for name in ("evaluation_result.json", "evaluation_report.md", "formal_metrics.csv", "noninferiority.csv", "pareto_profiles.csv", "health_check.json", "trial_ledger.csv"):
        assert (artifacts / name).is_file()
    repeated = evaluate_experiment(context, "0903_TEST", runner=fake_runner)
    assert repeated.result == result.result


def test_experiment_path_must_be_direct_child(tmp_path):
    write_bundle(tmp_path)
    context = RepositoryContext.discover(tmp_path, explicit_root=tmp_path)
    try:
        evaluate_experiment(context, "../0903_TEST", runner=fake_runner)
    except ValueError as exc:
        assert "experiment" in str(exc)
    else:
        raise AssertionError("path traversal accepted")


def test_strategy_evaluator_dependency_direction_is_one_way():
    root = Path(__file__).resolve().parents[1]
    forbidden = (root / "packages" / "strategy_manager", root / "packages" / "paper_trading_engine")
    assert not [path for directory in forbidden for path in directory.rglob("*.py") if "strategy_evaluator" in path.read_text(encoding="utf-8")]


def test_accept_freezes_once_and_retries_only_pending_pte(tmp_path):
    experiment = write_bundle(tmp_path)
    repository_root = Path(__file__).resolve().parents[1]
    shutil.copytree(repository_root / "configs" / "strategies", tmp_path / "configs" / "strategies")
    context = RepositoryContext.discover(tmp_path, explicit_root=tmp_path)
    evaluate_experiment(context, "0903_TEST", runner=fake_runner)

    calls = []

    def pte_runner(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="temporarily unavailable")
        return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "PASS"}), stderr="")

    first = accept_evaluation(context, "0903_TEST", "tester", "reviewed", pte_runner=pte_runner)
    second = accept_evaluation(context, "0903_TEST", "tester", "reviewed", pte_runner=pte_runner)
    third = accept_evaluation(context, "0903_TEST", "tester", "reviewed", pte_runner=pte_runner)
    assert first.result["activation_state"] == "PAPER_ACTIVATION_PENDING"
    assert second.result["activation_state"] == "PAPER_ACTIVE"
    assert third.result == second.result
    assert len(calls) == 2
    assert (tmp_path / "configs" / "strategies" / "S001" / "versions" / "v2.json").is_file()
    assert json.loads((experiment / "evaluation_acceptance.json").read_text(encoding="utf-8"))["release_id"] == "S001-v2"


def test_accept_rejects_non_freeze_decision(tmp_path):
    experiment = write_bundle(tmp_path)
    artifacts = experiment / "artifacts"
    artifacts.mkdir()
    (artifacts / "evaluation_result.json").write_text(json.dumps({"decision": "KEEP_INCUMBENT", "input_hash": "x"}), encoding="utf-8")
    context = RepositoryContext.discover(tmp_path, explicit_root=tmp_path)
    try:
        accept_evaluation(context, "0903_TEST", "tester", "reviewed")
    except ValueError as exc:
        assert "RECOMMEND_FREEZE" in str(exc)
    else:
        raise AssertionError("non-freeze result accepted")
