from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.evaluation_service import (
    accept_evaluation,
    evaluate_experiment,
)
from strategy_evaluator import (
    AuditIdentity,
    AuditStatus,
    ChampionAuditResult,
    MachineEvaluationReport,
    MachineVerdict,
    MetricObservation,
    MetricStatus,
    ReturnMatrixEvidence,
    RiskLabel,
)
from strategy_manager import canonical_sha256


def _runtime_pass(version):
    release_hash = version.release_hash or canonical_sha256(version.release_payload())
    return {
        "schema_version": 1,
        "status": "PASS",
        "release_id": version.release_id,
        "release_hash": release_hash,
        "runtime_sha256": "f" * 64,
    }


def _write_evaluation_bundle(root: Path) -> Path:
    experiment = root / "experiments" / "S001" / "0904_TEST"
    experiment.mkdir(parents=True)
    protocol = {
        "schema_version": 1,
        "standard_version": "opc-v3",
        "experiment_id": "0904_TEST",
        "research_objective": "improve range",
        "development_cutoff": "2026-09-02",
        "incumbent_id": "S001-v2",
        "incumbent_hash": "a" * 64,
        "decision_windows": ["full"],
        "target_windows": ["full"],
        "execution_policy_hash": "b" * 64,
        "tightened_margins": {},
        "shortlist_limit": 12,
        "target_requirements": [
            {
                "metric": "full_return",
                "direction": "maximize",
                "minimum_improvement": 0.01,
            }
        ],
        "candidate_manifest": "candidate_manifest.json",
    }
    candidates = [
        {
            "candidate_id": "S001-v2",
            "strategy_hash": "a" * 64,
            "execution_policy_hash": "b" * 64,
            "behavior_hash": "incumbent-behavior",
            "is_incumbent": True,
            "strategy_payload": {"rule": {"enter": 1}},
        },
        {
            "candidate_id": "winner",
            "strategy_id": "S001",
            "strategy_name": "综合基线策略",
            "strategy_hash": "c" * 64,
            "execution_policy_hash": "b" * 64,
            "behavior_hash": "winner-behavior",
            "family": "range",
            "parameter_group": "weights",
            "parameter_distance": 0.1,
            "is_incumbent": False,
            "strategy_payload": {"rule": {"enter": 2}},
        },
        {
            "candidate_id": "z-duplicate",
            "strategy_id": "S001",
            "strategy_name": "综合基线策略",
            "strategy_hash": "d" * 64,
            "execution_policy_hash": "b" * 64,
            "behavior_hash": "winner-behavior",
            "family": "range",
            "parameter_group": "weights",
            "parameter_distance": 0.2,
            "is_incumbent": False,
            "strategy_payload": {"rule": {"enter": 3}},
        },
        {
            "candidate_id": "inferior",
            "strategy_id": "S001",
            "strategy_name": "综合基线策略",
            "strategy_hash": "e" * 64,
            "execution_policy_hash": "b" * 64,
            "behavior_hash": "inferior-behavior",
            "family": "range",
            "parameter_group": "weights",
            "parameter_distance": 0.3,
            "is_incumbent": False,
            "strategy_payload": {"rule": {"enter": 4}},
        },
    ]
    manifest = {
        "schema_version": 1,
        "symbol": "588080.SH",
        "asset_type": "etf",
        "fee_rate": 0.0005,
        "init_cash": 100000,
        "windows": {"full": {"start": "2021-01-04", "end": "2026-09-02"}},
        "candidates": candidates,
        "trials": [
            {
                "trial_id": f"trial-{row['candidate_id']}",
                "candidate_id": row["candidate_id"],
                "strategy_hash": row["strategy_hash"],
                "behavior_hash": row["behavior_hash"],
                "status": "COMPLETED",
            }
            for row in candidates
        ],
    }
    (experiment / "evaluation_protocol.json").write_text(
        json.dumps(protocol), encoding="utf-8"
    )
    (experiment / "candidate_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return experiment


def _fixed_runner(
    context,
    protocol,
    candidates,
    candidate_ids,
    tier,
    scenarios=("standard",),
):
    rows = []
    for candidate_id in candidate_ids:
        better = candidate_id == "winner"
        worse = candidate_id == "inferior"
        for scenario in scenarios:
            rows.append(
                MetricObservation(
                    candidate_id,
                    "full",
                    scenario,
                    tier,
                    0.11 if better else (0.01 if worse else 0.10),
                    0.55 if better else (0.05 if worse else 0.50),
                    -0.10 if not worse else -0.30,
                    1.1 if better else (0.03 if worse else 1.0),
                    MetricStatus.VALID,
                    2.1 if better else (0.5 if worse else 2.0),
                    MetricStatus.VALID,
                    20,
                    2.0,
                    0.01,
                    (("full_return", 0.55 if better else (0.05 if worse else 0.50)),),
                )
            )
    return tuple(rows)


def test_ft_t06_evaluation_audits_every_candidate_and_freezes_once(
    functional_repo: Path, monkeypatch
) -> None:
    experiment = _write_evaluation_bundle(functional_repo)
    context = RepositoryContext.discover(functional_repo, explicit_root=functional_repo)
    identity = AuditIdentity(
        "0904_TEST",
        "a" * 64,
        "2026-09-02",
        "b" * 64,
        "b" * 64,
        "opc-v3",
        "champion-audit-v1",
        7,
    )
    matrix = ReturnMatrixEvidence(
        ("2026-09-01", "2026-09-02"),
        ("winner", "S001-v2"),
        ((0.01, 0.0), (-0.01, 0.01)),
        "d" * 64,
    )
    sentinel = SimpleNamespace(search_returns=matrix, comparison_returns=matrix)
    audit = ChampionAuditResult(
        identity,
        "winner",
        AuditStatus.PASS,
        RiskLabel.MIXED,
        (),
        (),
        direction_flags=(("bootstrap", "MIXED"),),
    )
    report_payload = {
        "schema_version": 2,
        "report_id": "SE-0904_TEST-winner",
        "candidate_id": "winner",
        "candidate_hash": "c" * 64,
        "policy_id": "OPC-MACHINE-ELIGIBILITY",
        "policy_version": "v1",
        "evaluator_version": "machine-evaluation-v1",
        "risk_label": "MIXED",
        "checks": [],
        "machine_verdict": "ELIGIBLE_FOR_FREEZE_REVIEW",
        "reason_codes": [],
        "evidence_hash": "e" * 64,
        "audit_result": audit.to_dict(),
    }
    machine_report = MachineEvaluationReport(
        2,
        "SE-0904_TEST-winner",
        "winner",
        "c" * 64,
        "OPC-MACHINE-ELIGIBILITY",
        "v1",
        "machine-evaluation-v1",
        RiskLabel.MIXED,
        (),
        MachineVerdict.ELIGIBLE_FOR_FREEZE_REVIEW,
        (),
        "e" * 64,
        audit,
        canonical_sha256(report_payload),
    )
    monkeypatch.setattr(
        "czsc_trader.application.evaluation_service.build_champion_audit_request",
        lambda **_kwargs: sentinel,
    )
    monkeypatch.setattr(
        "czsc_trader.application.evaluation_service.evaluate_machine_eligibility",
        lambda request: machine_report
        if request.audit_request is sentinel
        else (_ for _ in ()).throw(AssertionError("unexpected audit request")),
    )

    result = evaluate_experiment(context, "0904_TEST", runner=_fixed_runner)
    repeated = evaluate_experiment(context, "0904_TEST", runner=_fixed_runner)
    artifacts = experiment / "artifacts"
    required = {
        "evaluation_result.json",
        "evaluation_report.md",
        "screening_metrics.csv",
        "screening_noninferiority.csv",
        "screening_decisions.csv",
        "formal_metrics.csv",
        "noninferiority.csv",
        "pareto_profiles.csv",
        "health_check.json",
        "trial_ledger.csv",
        "statistical_audit.json",
        "candidate_returns.csv",
        "comparison_returns.csv",
        "machine_evaluation.json",
    }
    present = {path.name for path in artifacts.iterdir()}
    assert required <= present, (result.result, sorted(present))
    assert result.result["decision"] == "RECOMMEND_FREEZE"
    assert repeated.result == result.result
    with (artifacts / "screening_decisions.csv").open(
        encoding="utf-8", newline=""
    ) as stream:
        decisions = {row["candidate_id"]: row for row in csv.DictReader(stream)}
    assert set(decisions) == {"winner", "z-duplicate", "inferior"}
    assert decisions["winner"]["outcome"] == "SHORTLISTED"
    assert decisions["z-duplicate"]["outcome"] == "BEHAVIOR_DEDUPLICATED"
    assert decisions["inferior"]["outcome"] == "SCREENING_NONINFERIORITY"
    stored_audit = json.loads(
        (artifacts / "statistical_audit.json").read_text(encoding="utf-8")
    )
    assert stored_audit["status"] == "PASS"
    assert stored_audit["risk_label"] == "MIXED"

    decisions_path = artifacts / "screening_decisions.csv"
    original_decisions = decisions_path.read_bytes()
    decisions_path.write_bytes(original_decisions + b"tampered\n")
    try:
        evaluate_experiment(context, "0904_TEST", runner=_fixed_runner)
    except ValueError as exc:
        assert "hash mismatch" in str(exc)
    else:
        raise AssertionError("tampered evaluation artifact was accepted")
    decisions_path.write_bytes(original_decisions)

    calls = []

    def fail_then_succeed(command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            return SimpleNamespace(
                returncode=1, stdout="", stderr="temporarily unavailable"
            )
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"status": "PASS"}), stderr=""
        )

    versions_dir = functional_repo / "strategies" / "S001" / "versions"
    versions_before = set(versions_dir.glob("v*.json"))
    review_path = experiment / "freeze_review.json"
    review_path.write_text(json.dumps({
        "schema_version": 1,
        "strategy_id": "S001",
        "candidate_id": "winner",
        "candidate_hash": "c" * 64,
        "decision": "APPROVE_FREEZE",
        "reviewed_by": "tester",
        "rationale": "functional acceptance",
        "mechanism_review": "APPROVED",
        "external_relevance_review": "APPROVED",
        "deployment_review": "APPROVED",
        "monitoring_plan_review": "APPROVED",
        "reviewed_at": "2026-09-16T10:00:00+08:00",
    }), encoding="utf-8")
    rejected_review = json.loads(review_path.read_text(encoding="utf-8"))
    rejected_review["deployment_review"] = "PENDING"
    rejected_review_path = experiment / "rejected_freeze_review.json"
    rejected_review_path.write_text(json.dumps(rejected_review), encoding="utf-8")
    with pytest.raises(ValueError, match="deployment_review"):
        accept_evaluation(
            context,
            "0904_TEST",
            "tester",
            "functional acceptance",
            rejected_review_path,
            pte_runner=fail_then_succeed,
            runtime_validator=_runtime_pass,
        )
    assert set(versions_dir.glob("v*.json")) == versions_before

    runtime_checks = []

    def reject_runtime(version):
        runtime_checks.append(version.release_id)
        raise ValueError("strategy implementation is unavailable")

    with pytest.raises(ValueError, match="implementation is unavailable"):
        accept_evaluation(
            context,
            "0904_TEST",
            "tester",
            "functional acceptance",
            review_path,
            pte_runner=fail_then_succeed,
            runtime_validator=reject_runtime,
        )
    research_versions = set(versions_dir.glob("v*.json")) - versions_before
    assert len(research_versions) == 1
    assert json.loads(next(iter(research_versions)).read_text(encoding="utf-8"))["release_hash"] is None
    assert len(runtime_checks) == 1
    assert runtime_checks[0].startswith("S001-v")
    assert calls == []

    first = accept_evaluation(
        context,
        "0904_TEST",
        "tester",
        "functional acceptance",
        review_path,
        pte_runner=fail_then_succeed,
        runtime_validator=_runtime_pass,
    )
    second = accept_evaluation(
        context,
        "0904_TEST",
        "tester",
        "functional acceptance",
        review_path,
        pte_runner=fail_then_succeed,
        runtime_validator=_runtime_pass,
    )
    third = accept_evaluation(
        context,
        "0904_TEST",
        "tester",
        "functional acceptance",
        review_path,
        pte_runner=fail_then_succeed,
        runtime_validator=_runtime_pass,
    )
    versions_after = set(versions_dir.glob("v*.json"))
    assert first.result["activation_state"] == "PAPER_ACTIVATION_PENDING"
    assert first.result["runtime_acceptance"]["release_hash"] == first.result["release_hash"]
    assert second.result["activation_state"] == "PAPER_ACTIVE"
    assert third.result == second.result
    assert len(calls) == 2
    assert len(versions_after - versions_before) == 1
