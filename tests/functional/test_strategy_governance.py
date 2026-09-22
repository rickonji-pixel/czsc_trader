from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from strategy_manager import (
    AdjudicationReport,
    CandidateSnapshot,
    EvaluationMandate,
    GovernanceResult,
    GovernanceStage,
    StrategyFamily,
    StrategyRegistry,
    canonical_sha256,
)
from trading_execution_engine import EXECUTION_CONTRACT_VERSION

from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.freeze_review_service import (
    _assert_formal_evaluation_contract,
    _hash_file,
    _load_protocol_and_candidate,
    _runtime_audit,
    _validate_mandate_contract,
    evaluate_freeze_review,
    freeze_review_candidate,
    open_freeze_review,
)
from czsc_trader.application.errors import ValidationError
from czsc_trader.application.freeze_review_service import _candidate_runtime, _submitted_runtime
from czsc_trader.application.runtime_acceptance import validate_candidate_readiness
from czsc_trader.application.results import CommandResult
from czsc_trader.application.research_governance_service import (
    create_research_batch,
    update_research_intent,
)
from functional_support import invoke_main


def _write_json(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _hashed(value: dict, field: str) -> dict:
    return {**value, field: canonical_sha256(value)}


def _audit_requirements(*, required_external_replays: int = 0) -> dict:
    return {
        "parameter_robustness": {"minimum_valid_neighbors": 1},
        "statistical_robustness": {
            "allowed_risk_labels": ["FAVORABLE", "MIXED"],
            "minimum_bootstrap_probability": 0.5,
            "maximum_pbo": 0.5,
            "minimum_dsr_probability": 0.5,
        },
        "technical_replay": {
            "mode": "FULL_RECOMPUTE",
            "allow_artifact_reuse": False,
            "execution_engine": EXECUTION_CONTRACT_VERSION,
        },
        "external_validation": {
            "required_replays": required_external_replays,
            "minimum_cagr": 0.0,
            "max_drawdown_floor": -1.0,
        },
        "runtime_acceptance": {"required_status": "PASS"},
        "monitoring_plan": {"required_status": "APPROVED", "minimum_rules": 1},
    }


def _cost_policy() -> dict:
    return {
        "primary_fee_rate": 0.001,
        "stress_scenarios": [
            "total_cost_15bp",
            "total_cost_20bp",
            "total_cost_30bp",
            "total_cost_50bp",
        ],
        "blocking_scenarios": ["total_cost_15bp"],
        "minimum_cagr": 0.0,
        "max_drawdown_floor": -1.0,
        "minimum_calmar": 0.0,
    }


def _complete_audits() -> list[str]:
    return [
        "objective_recalculation",
        "frequency_recalculation",
        "parameter_robustness",
        "statistical_robustness",
        "cost_stress",
        "technical_replay",
        "external_validation",
        "runtime_acceptance",
        "monitoring_plan",
    ]


def _mandate_for_contract_test(**changes) -> EvaluationMandate:
    payload = {
        "schema_version": 1,
        "mandate_id": "EM-S999-C001-001",
        "strategy_id": "S999",
        "candidate_id": "C001",
        "development_cutoff": "2026-09-02",
        "forward_start": "2026-09-03",
        "evaluation_windows": {"full": {"start": "2021-01-04", "end": "2026-09-02"}},
        "benchmark": {"type": "strategy", "id": "BuyHold"},
        "objectives": [{"metric": "annual_return", "operator": ">=", "value": 0.15}],
        "cost_policy": _cost_policy(),
        "frequency_policy": {"mode": "OBSERVE", "window_days": 60},
        "audit_requirements": _audit_requirements(),
        "required_audits": _complete_audits(),
        "evidence_seen_through": "2026-09-02",
        "finalized_at": "2026-09-18T11:00:00+08:00",
        "finalized_by": "tester",
    }
    payload.update(changes)
    return EvaluationMandate.from_dict(_hashed(payload, "mandate_hash"))


def test_tdr_rejects_incomplete_audit_matrix_and_stale_formal_contract() -> None:
    incomplete = _mandate_for_contract_test(
        required_audits=_complete_audits()[:-1],
    )
    with pytest.raises(ValueError, match="complete audit matrix"):
        _validate_mandate_contract(incomplete)

    mandate = _mandate_for_contract_test()
    policy = {
        "policy_type": "FROZEN_RULE",
        "settings": {"buy": "LIMIT", "sell": "MARKET"},
    }
    snapshot_payload = {
        "schema_version": 1,
        "strategy_id": "S999",
        "candidate_id": "C001",
        "source_experiment": "experiments/S999/TEST",
        "strategy_payload": {"symbol": "588080.SH"},
        "data_contract": {
            "symbol": "588080.SH",
            "asset_type": "etf",
            "requirements": [{"name": "market"}],
        },
        "execution_policy": policy,
        "research_claims": {"annual_return": 0.15},
    }
    snapshot = CandidateSnapshot.from_dict(_hashed(snapshot_payload, "candidate_hash"))
    stale = CommandResult(
        "PASS",
        "strategy.evaluate",
        {
            "formal_evaluation_contract": {
                "development_cutoff": "2026-09-01",
                "windows": mandate.evaluation_windows,
                "benchmark_id": "BuyHold",
                "primary_fee_rate": 0.001,
                "stress_scenarios": mandate.cost_policy["stress_scenarios"],
                "frequency_window_days": 60,
                "execution_policy_hash": canonical_sha256(policy),
                "execution_engine": EXECUTION_CONTRACT_VERSION,
                "artifact_reuse": False,
            }
        },
    )
    with pytest.raises(ValueError, match="differs from evaluation mandate"):
        _assert_formal_evaluation_contract(stale, mandate, snapshot, "d" * 64)
    from czsc_trader.candidate_evaluation import METRIC_SEMANTICS_VERSION
    contract = stale.result["formal_evaluation_contract"]
    contract.update(development_cutoff=mandate.development_cutoff,
                    metric_semantics_version=METRIC_SEMANTICS_VERSION, review_data_hash="d" * 64)
    _assert_formal_evaluation_contract(stale, mandate, snapshot, "d" * 64)
    contract["metric_semantics_version"] = "legacy"
    with pytest.raises(ValueError, match="differs from evaluation mandate"):
        _assert_formal_evaluation_contract(stale, mandate, snapshot, "d" * 64)


def test_tdr_rejects_execution_contract_false_success(tmp_path: Path) -> None:
    declared = {
        "policy_type": "FROZEN_RULE",
        "settings": {"buy": "LIMIT", "sell": "MARKET", "fee_rate": 0.001},
    }
    snapshot_payload = {
        "schema_version": 1,
        "strategy_id": "S999",
        "candidate_id": "C001",
        "source_experiment": "experiments/S999/TEST",
        "strategy_payload": {
            "symbol": "588080.SH",
            "rule": {
                "execution": {"buy": "LIMIT", "sell": "LIMIT", "fee_rate": 0.001}
            },
        },
        "data_contract": {
            "symbol": "588080.SH",
            "asset_type": "etf",
            "requirements": [{"name": "market"}],
        },
        "execution_policy": declared,
        "research_claims": {"annual_return": 0.15},
    }
    snapshot = CandidateSnapshot.from_dict(_hashed(snapshot_payload, "candidate_hash"))
    _write_json(
        tmp_path / "evaluation_protocol.json",
        {
            "schema_version": 1,
            "standard_version": "opc-v3",
            "experiment_id": "TEST",
            "research_objective": "execution contract",
            "development_cutoff": "2026-09-02",
            "incumbent_id": "BuyHold",
            "incumbent_hash": "a" * 64,
            "decision_windows": ["full"],
            "target_windows": ["full"],
            "execution_policy_hash": canonical_sha256(declared),
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
        },
    )
    _write_json(
        tmp_path / "candidate_manifest.json",
        {
            "candidates": [
                {
                    "candidate_id": "C001",
                    "strategy_payload": snapshot.strategy_payload,
                    "execution_policy_hash": canonical_sha256(declared),
                }
            ]
        },
    )
    # TDR no longer parses rule.execution; the loaded SRT is authoritative.
    _load_protocol_and_candidate(tmp_path, snapshot)

    runtime = {
        "status": "PASS",
        "input_contract": {"requirements": snapshot.data_contract["requirements"]},
        "execution_policy": {
            "policy_type": "FROZEN_RULE",
            "settings": {"buy": "MARKET", "sell": "MARKET", "fee_rate": 0.001},
        },
    }
    assert _runtime_audit(snapshot, runtime)["status"] == "FAIL"


def test_research_family_can_start_a_second_governed_batch(functional_repo: Path) -> None:
    context = RepositoryContext.discover(functional_repo)
    first = _write_json(
        functional_repo / "first-batch.json",
        {
            "strategy_id": "S910",
            "name": "多批次研究策略",
            "scope": ["588080.SH"],
            "research_intent": {"objective": "建立首个候选"},
        },
    )
    create_research_batch(context, first, actor="tester", reason="批准首轮研究")
    second = _write_json(
        functional_repo / "second-batch.json",
        {
            "strategy_id": "S910",
            "name": "多批次研究策略",
            "scope": ["588080.SH"],
            "research_intent": {"objective": "研究下一冻结版本"},
            "credential_id": "SGC-S910-002",
        },
    )
    result = create_research_batch(
        context, second, actor="tester", reason="批准第二轮研究"
    )
    updated = update_research_intent(
        context,
        "S910",
        _write_json(
            functional_repo / "intent-update.json",
            {
                "research_intent": {"objective": "等待新数据继续研究"},
                "research_state": "PAUSED",
            },
        ),
        actor="tester",
        reason="暂停等待新数据",
    )

    registry = StrategyRegistry(functional_repo / "research" / "registrations")
    assert registry.get_family("S910").research_intent == {
        "objective": "等待新数据继续研究"
    }
    assert registry.get_governance_credential(
        "S910", "SGC-S910-001"
    ).stage is GovernanceStage.RESEARCH_INITIATED
    assert registry.get_governance_credential(
        "S910", "SGC-S910-002"
    ).stage is GovernanceStage.RESEARCH_INITIATED
    assert result.result["research_batch_document"] == (
        "research/S910/batches/SGC-S910-002.md"
    )
    assert updated.result["family"]["research_state"] == "PAUSED"
    assert not (functional_repo / "strategies" / "S910").exists()


def test_ft_t05_three_human_gates_create_only_one_frozen_version(
    functional_repo: Path, capsys, monkeypatch, candidate_payload
) -> None:
    initial_credential_count = StrategyRegistry(
        functional_repo / "strategies"
    ).validate_all()["credentials"]
    payload, package = candidate_payload
    from strategy_runtime import StrategyCandidate
    from strategy_runtime.loader import StrategyLoader
    from czsc_trader.application.runtime_acceptance import _runtime_report
    loaded = StrategyLoader().load_candidate(
        StrategyCandidate("S900", "C001", payload, package)
    )
    readiness = _runtime_report(loaded.definition)
    requirements = readiness["input_contract"]["requirements"]
    execution_policy = readiness["execution_policy"]
    root = ["--repo-root", str(functional_repo)]
    family = _write_json(
        functional_repo / "research-batch.json",
        {
            "strategy_id": "S900",
            "name": "功能测试策略",
            "scope": ["588080.SH"],
            "research_intent": {"objective": "验证可信策略治理链路"},
        },
    )
    created = invoke_main(
        [
            "research",
            "create",
            "--input",
            str(family),
            "--actor",
            "tester",
            "--reason",
            "批准研究立项",
            *root,
        ],
        capsys,
    )

    snapshot_payload = {
        "schema_version": 1,
        "strategy_id": "S900",
        "candidate_id": "C001",
        "source_experiment": "experiments/S900/0904_TEST",
        "strategy_payload": payload,
        "data_contract": {
            "symbol": "588080.SH",
            "asset_type": "etf",
            "requirements": requirements,
        },
        "execution_policy": execution_policy,
        "research_claims": {"annual_return": 0.20, "maximum_drawdown": -0.18},
    }
    snapshot = _write_json(
        functional_repo / "candidate-snapshot.json",
        _hashed(snapshot_payload, "candidate_hash"),
    )
    mandate_payload = {
        "schema_version": 1,
        "mandate_id": "EM-S900-C001-001",
        "strategy_id": "S900",
        "candidate_id": "C001",
        "development_cutoff": "2026-09-02",
        "forward_start": "2026-09-03",
        "evaluation_windows": {"full": {"start": "2021-01-01", "end": "2026-09-02"}},
        "benchmark": {"type": "strategy", "id": "S001-v2"},
        "objectives": [
            {"metric": "annual_return", "operator": ">=", "value": 0.15},
            {"metric": "maximum_drawdown", "operator": ">=", "value": -0.20},
        ],
        "cost_policy": _cost_policy(),
        "frequency_policy": {"mode": "OBSERVE", "window_days": 60},
        "audit_requirements": _audit_requirements(),
        "required_audits": _complete_audits(),
        "evidence_seen_through": "2026-09-02",
        "finalized_at": "2026-09-18T11:00:00+08:00",
        "finalized_by": "tester",
    }
    mandate = _write_json(
        functional_repo / "evaluation-mandate.json",
        _hashed(mandate_payload, "mandate_hash"),
    )
    experiment = functional_repo / "experiments" / "S900" / "0904_TEST"
    artifacts = experiment / "artifacts"
    artifacts.mkdir(parents=True)
    _write_json(
        experiment / "evaluation_protocol.json",
        {
            "schema_version": 1,
            "standard_version": "opc-v3",
            "experiment_id": "0904_TEST",
            "research_objective": "functional lifecycle",
            "development_cutoff": "2026-09-02",
            "incumbent_id": "S001-v2",
            "incumbent_hash": "a" * 64,
            "decision_windows": ["full"],
            "target_windows": ["full"],
            "execution_policy_hash": canonical_sha256(execution_policy),
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
        },
    )
    _write_json(
        experiment / "candidate_manifest.json",
        {
            "symbol": "588080.SH",
            "fee_rate": 0.001,
            "init_cash": 100000.0,
            "forward_start": "2026-09-03",
            "windows": mandate_payload["evaluation_windows"],
            "candidates": [
                {
                    "candidate_id": "C001",
                    "strategy_payload": snapshot_payload["strategy_payload"],
                    "execution_policy_hash": canonical_sha256(execution_policy),
                }
            ],
            "trials": [],
        },
    )
    (artifacts / "formal_metrics.csv").write_text(
        "candidate_id,window_id,scenario_id,net_cagr,max_drawdown,calmar,profit_factor,profit_factor_status,total_return,closed_trades\n"
        "C001,full,standard,0.20,-0.18,1.11,1.30,VALID,0.42,35\n",
        encoding="utf-8",
    )
    _write_json(artifacts / "evaluation_result.json", {"verified": True})
    (artifacts / "parameter_neighborhood.csv").write_text(
        "candidate_id,calmar\nC001,1.11\n", encoding="utf-8"
    )
    (artifacts / "execution_stress.csv").write_text(
        "scenario_id,calmar\ntotal_cost_15bp,0.90\n", encoding="utf-8"
    )
    _write_json(artifacts / "statistical_audit.json", {"risk_label": "MIXED"})
    _write_json(
        artifacts / "external_validation.json",
        {"candidate_id": "C001", "replays": []},
    )
    _write_json(
        artifacts / "monitoring_plan.json",
        {"status": "APPROVED", "rules": [{"metric": "drawdown"}]},
    )

    missing_runtime = deepcopy(snapshot_payload)
    del missing_runtime["strategy_payload"]["runtime"]
    missing_path = _write_json(
        functional_repo / "missing-runtime.json", _hashed(missing_runtime, "candidate_hash"),
    )
    manifest_path = experiment / "candidate_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    missing_manifest = deepcopy(manifest)
    missing_manifest["candidates"][0]["strategy_payload"] = missing_runtime["strategy_payload"]
    _write_json(manifest_path, missing_manifest)
    with pytest.raises(ValidationError) as rejected:
        open_freeze_review(
            RepositoryContext.discover(functional_repo),
            credential_id="SGC-S900-001",
            candidate_path=missing_path,
            mandate_path=mandate,
            actor="tester",
            reason="missing implementation",
            runtime_root=package,
        )
    assert rejected.value.code == "freeze_review_open_failed"
    research_registry = StrategyRegistry(
        functional_repo / "research" / "registrations"
    )
    assert research_registry.get_governance_credential(
        "S900", "SGC-S900-001"
    ).stage is GovernanceStage.RESEARCH_INITIATED
    assert not (functional_repo / "strategies" / "S900").exists()
    _write_json(manifest_path, manifest)

    opened = open_freeze_review(
        RepositoryContext.discover(functional_repo),
        credential_id="SGC-S900-001",
        candidate_path=snapshot,
        mandate_path=mandate,
        actor="tester",
        reason="批准候选进入冻结流程",
        runtime_root=package,
    )
    opened_replay = open_freeze_review(
        RepositoryContext.discover(functional_repo),
        credential_id="SGC-S900-001",
        candidate_path=snapshot,
        mandate_path=mandate,
        actor="tester",
        reason="批准候选进入冻结流程",
        runtime_root=package,
    )
    registry = StrategyRegistry(functional_repo / "strategies")
    credential = registry.get_governance_credential("S900", "SGC-S900-001")
    submission = credential.seals[-1]
    stored_snapshot = CandidateSnapshot.from_dict(submission.content["candidate_snapshot"])
    stored_mandate = EvaluationMandate.from_dict(submission.content["evaluation_mandate"])
    runtime = _candidate_runtime(stored_snapshot, package)
    assert runtime["identity_kind"] == "CANDIDATE"
    assert runtime["release_id"] == "S900-C001"
    assert registry.versions("S900") == ()
    assert submission.content["candidate_runtime"] == runtime
    assert _submitted_runtime(submission, stored_snapshot, package) == runtime
    source = package / "strategies" / "candidate_fixture.py"
    original_source = source.read_bytes()
    source.write_bytes(original_source + b"\n# changed after submission\n")
    from strategy_runtime import RuntimeCompatibilityError
    with pytest.raises(RuntimeCompatibilityError, match="source hash"):
        _submitted_runtime(submission, stored_snapshot, package)
    source.write_bytes(original_source)
    file_audits = {
        "objective_recalculation": "formal_metrics.csv",
        "frequency_recalculation": "formal_metrics.csv",
        "parameter_robustness": "parameter_neighborhood.csv",
        "statistical_robustness": "statistical_audit.json",
        "cost_stress": "execution_stress.csv",
        "external_validation": "external_validation.json",
        "monitoring_plan": "monitoring_plan.json",
    }
    audit_results = {
        name: {
            "status": "PASS",
            "artifact": artifact,
            "evidence_hash": _hash_file(artifacts / artifact),
        }
        for name, artifact in file_audits.items()
    }
    audit_results["runtime_acceptance"] = {
        "status": "PASS",
        "evidence_hash": canonical_sha256(runtime),
    }
    audit_results["technical_replay"] = {
        "status": "PASS",
        "artifact": "evaluation_result.json",
        "artifact_hash": _hash_file(artifacts / "evaluation_result.json"),
        "evidence_hash": "d" * 64,
    }
    report_payload = {
        "schema_version": 1,
        "report_id": "ADR-SGC-S900-001-2",
        "review_id": "SGC-S900-001",
        "strategy_id": "S900",
        "candidate_id": stored_snapshot.candidate_id,
        "candidate_hash": stored_snapshot.candidate_hash,
        "evaluation_mandate_hash": stored_mandate.mandate_hash,
        "audit_policy_hash": canonical_sha256(submission.content["audit_policy"]),
        "claim_checks": [],
        "audit_results": audit_results,
        "machine_verdict": "ELIGIBLE_FOR_FREEZE_REVIEW",
        "risk_label": "MIXED",
        "blocking_findings": [],
        "reservations": ["functional"],
        "generated_at": "2026-09-18T12:00:00+08:00",
    }
    report = AdjudicationReport.from_dict(_hashed(report_payload, "report_hash"))
    registry.append_governance_seal(
        "S900",
        "SGC-S900-001",
        stage=GovernanceStage.TDR_ADJUDICATED,
        result=GovernanceResult.ELIGIBLE,
        actor="TDR",
        expected_previous_hash=credential.credential_hash,
        content={
            "submission_seal_hash": submission.seal_hash,
            "adjudication_report": report.to_dict(),
        },
        artifact_hashes={"adjudication_report": report.report_hash, "review_dataset": "d" * 64},
    )
    # A release factory must preserve reviewed contracts, not only its parameters.
    from strategy_runtime.models import MonitoringPolicy
    original_factory = type(loaded).from_release
    # This test isolates lifecycle/identity gates; snapshot publication is exercised
    # with real SRT + DFLS + TXE in test_candidate_runtime_execution.
    monkeypatch.setattr(
        "czsc_trader.application.freeze_review_service._verified_review_evidence",
        lambda *args: experiment,
    )

    def changed_release(cls, release):
        strategy = original_factory(release)
        strategy.definition = replace(
            strategy.definition, monitoring=MonitoringPolicy("CHANGED", {}),
        )
        return strategy

    with monkeypatch.context() as patch:
        patch.setattr(type(loaded), "from_release", classmethod(changed_release))
        with pytest.raises(ValidationError, match="monitoring_sha256"):
            freeze_review_candidate(
                RepositoryContext.discover(functional_repo), "S900", "SGC-S900-001",
                actor="tester", reason="approve", change_summary="first",
                runtime_root=package,
            )
    assert registry.versions("S900") == ()
    assert registry.get_governance_credential("S900", "SGC-S900-001").stage is GovernanceStage.TDR_ADJUDICATED
    frozen = freeze_review_candidate(
        RepositoryContext.discover(functional_repo),
        "S900",
        "SGC-S900-001",
        change_summary="首个冻结版本",
        actor="tester",
        reason="批准低成本模拟观察",
        runtime_root=package,
    )
    replay = freeze_review_candidate(
        RepositoryContext.discover(functional_repo),
        "S900",
        "SGC-S900-001",
        change_summary="首个冻结版本",
        actor="tester",
        reason="批准低成本模拟观察",
        runtime_root=package,
    )

    assert created["result"]["family"]["strategy_id"] == "S900"
    assert opened.result["governance_credential"]["stage"] == "CANDIDATE_SUBMITTED"
    assert len(opened_replay.result["governance_credential"]["seals"]) == 2
    assert frozen.result["version"]["release_id"] == "S900-v1"
    assert frozen.result["pte_deployment"] == "NOT_REQUESTED"
    assert replay.result["idempotent_replay"] is True
    assert len(registry.versions("S900")) == 1
    completed = registry.get_governance_credential("S900", "SGC-S900-001")
    assert [seal.stage.value for seal in completed.seals] == [
        "RESEARCH_INITIATED",
        "CANDIDATE_SUBMITTED",
        "TDR_ADJUDICATED",
        "FREEZE_APPROVED",
        "VERSION_FROZEN",
    ]
    assert completed.seals[-1].content["release_id"] == "S900-v1"
    assert not (functional_repo / "strategies" / "S900" / "reviews").exists()
    assert registry.validate_all()["credentials"] == initial_credential_count + 1
    assert [event.event_type for event in registry.lifecycle_events("S900")] == [
        "RESEARCH_BATCH_CREATED",
        "VERSION_FROZEN",
    ]


def test_ft_t05_old_direct_creation_commands_are_absent() -> None:
    from czsc_trader.cli.main import build_parser

    strategy = next(
        action
        for action in build_parser()._actions
        if action.__class__.__name__ == "_SubParsersAction"
    ).choices["strategy"]
    actions = next(
        action for action in strategy._actions if action.__class__.__name__ == "_SubParsersAction"
    ).choices
    assert "create" not in actions
    assert "version" not in actions
    assert "evaluate" not in actions
    assert "accept-evaluation" not in actions


def test_ft_t06_tdr_recomputes_every_required_audit_and_rejects_false_claim(
    functional_repo: Path, monkeypatch, candidate_payload
) -> None:
    context = RepositoryContext.discover(functional_repo)
    registry = StrategyRegistry(context.strategy_root)
    registry.create_family(
        StrategyFamily.from_dict(
            {
                "schema_version": 2,
                "strategy_id": "S901",
                "name": "独立复核测试策略",
                "scope": ["588080.SH"],
                "research_intent": {"objective": "验证TDR独立复核"},
                "research_state": "RESEARCHING",
                "created_at": "2026-09-18T10:00:00+08:00",
                "created_by": "tester",
                "updated_at": "2026-09-18T10:00:00+08:00",
            }
        ),
        actor="tester",
        reason="批准研究立项",
        credential_id="SGC-S901-001",
        credential_content={"research_intent": "验证TDR独立复核"},
    )
    payload, package = candidate_payload
    from strategy_runtime import StrategyCandidate, StrategyRuntime
    from czsc_trader.application.runtime_acceptance import _runtime_report
    readiness = _runtime_report(
        StrategyRuntime().describe(
            StrategyCandidate("S901", "C001", payload, package)
        )
    )
    requirements = readiness["input_contract"]["requirements"]
    policy = readiness["execution_policy"]
    snapshot_raw = {
        "schema_version": 1,
        "strategy_id": "S901",
        "candidate_id": "C001",
        "source_experiment": "experiments/S901/0918_TEST",
        "strategy_payload": payload,
        "data_contract": {
            "symbol": "588080.SH",
            "asset_type": "etf",
            "requirements": requirements,
        },
        "execution_policy": policy,
        "research_claims": {"annual_return": 100_000.0, "maximum_drawdown": -0.18},
    }
    snapshot = CandidateSnapshot.from_dict(_hashed(snapshot_raw, "candidate_hash"))
    candidate_runtime = validate_candidate_readiness(snapshot, source_root=package)
    audits = [
        "objective_recalculation",
        "frequency_recalculation",
        "parameter_robustness",
        "statistical_robustness",
        "cost_stress",
        "technical_replay",
        "external_validation",
        "runtime_acceptance",
        "monitoring_plan",
    ]
    mandate_raw = {
        "schema_version": 1,
        "mandate_id": "EM-S901-C001-001",
        "strategy_id": "S901",
        "candidate_id": "C001",
        "development_cutoff": "2026-09-02",
        "forward_start": "2026-09-03",
        "evaluation_windows": {"full": {"start": "2021-01-01", "end": "2026-09-02"}},
        "benchmark": {"type": "strategy", "id": "S001-v2"},
        "objectives": [
            {"metric": "annual_return", "operator": ">=", "value": 0.15},
            {"metric": "maximum_drawdown", "operator": ">=", "value": -0.20},
        ],
        "cost_policy": _cost_policy(),
        "frequency_policy": {"mode": "OBSERVE", "window_days": 60},
        "audit_requirements": _audit_requirements(),
        "required_audits": audits,
        "evidence_seen_through": "2026-09-02",
        "finalized_at": "2026-09-18T11:00:00+08:00",
        "finalized_by": "tester",
    }
    mandate = EvaluationMandate.from_dict(_hashed(mandate_raw, "mandate_hash"))
    credential = registry.get_governance_credential("S901", "SGC-S901-001")
    audit_policy = {
            "policy_version": "tdr-freeze-v3",
            "required_audits": mandate.required_audits,
            "requirements": mandate.audit_requirements,
    }
    experiment = context.experiments_root / "S901" / "0918_TEST"
    artifacts = experiment / "artifacts"
    artifacts.mkdir(parents=True)
    _write_json(
        experiment / "evaluation_protocol.json",
        {
            "schema_version": 1,
            "standard_version": "opc-v3",
            "experiment_id": "0918_TEST",
            "research_objective": "independent adjudication",
            "development_cutoff": "2026-09-02",
            "incumbent_id": "S001-v2",
            "incumbent_hash": "a" * 64,
            "decision_windows": ["full"],
            "target_windows": ["full"],
            "execution_policy_hash": canonical_sha256(policy),
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
        },
    )
    _write_json(
        experiment / "candidate_manifest.json",
        {
            "symbol": "588080.SH",
            "fee_rate": 0.001,
            "init_cash": 100000.0,
            "forward_start": "2026-09-03",
            "windows": mandate.evaluation_windows,
            "candidates": [
                {
                    "candidate_id": "C001",
                    "strategy_payload": snapshot.strategy_payload,
                    "execution_policy_hash": canonical_sha256(policy),
                }
            ],
            "trials": [],
        },
    )
    (artifacts / "formal_metrics.csv").write_text(
        "candidate_id,window_id,scenario_id,net_cagr,max_drawdown,calmar,profit_factor,profit_factor_status,total_return,closed_trades,frequency_window_days,rolling_closed_trades_median,rolling_closed_trades_p10\n"
        "C001,full,standard,0.20,-0.18,1.11,1.30,VALID,0.42,35,60,7,3\n",
        encoding="utf-8",
    )
    (artifacts / "parameter_neighborhood.csv").write_text(
        "candidate_id,calmar\nC001,1.11\n", encoding="utf-8"
    )
    (artifacts / "execution_stress.csv").write_text(
        "scenario_id,calmar\ntotal_cost_15bp,0.90\n", encoding="utf-8"
    )
    _write_json(artifacts / "statistical_audit.json", {"risk_label": "MIXED"})
    _write_json(artifacts / "evaluation_result.json", {"verified": True})
    _write_json(
        artifacts / "external_validation.json",
        {
            "status": "PASS",
            "candidate_id": snapshot.candidate_id,
            "candidate_hash": snapshot.candidate_hash,
            "replays": [],
        },
    )
    _write_json(
        artifacts / "monitoring_plan.json",
        {"status": "APPROVED", "rules": [{"metric": "drawdown", "operator": "<"}]},
    )

    from czsc_trader.application.freeze_review_service import _evaluation_inputs
    from czsc_trader.candidate_evaluation import METRIC_SEMANTICS_VERSION
    protocol, manifest, _ = _load_protocol_and_candidate(experiment, snapshot)
    inputs = _evaluation_inputs(experiment, protocol, manifest)
    registry.append_governance_seal(
        "S901", "SGC-S901-001", stage=GovernanceStage.CANDIDATE_SUBMITTED,
        result=GovernanceResult.OPEN, actor="tester", expected_previous_hash=credential.credential_hash,
        content={
            "submission_id": "SUB-SGC-S901-001-2", "candidate_snapshot": snapshot.to_dict(),
            "evaluation_mandate": mandate.to_dict(), "audit_policy": audit_policy,
            "candidate_runtime": candidate_runtime, "evaluation_inputs": inputs,
        },
        artifact_hashes={
            "candidate_snapshot": canonical_sha256(snapshot.to_dict()),
            "evaluation_mandate": mandate.mandate_hash, "audit_policy": canonical_sha256(audit_policy),
            "candidate_runtime": canonical_sha256(candidate_runtime), "evaluation_inputs": canonical_sha256(inputs),
        },
    )
    # Orchestration test stubs data ingestion and numerical assessment; it asserts
    # isolated destinations and sealed input propagation, not statistical validity.
    monkeypatch.setattr("czsc_trader.application.freeze_review_service.publish_review_dataset",
                        lambda *args, **kwargs: {"snapshot_hash": "d" * 64})
    monkeypatch.setattr("czsc_trader.application.freeze_review_service.verify_review_dataset",
                        lambda *args: {"snapshot_hash": "d" * 64})
    calls = []

    def recompute(_context, experiment_id, **kwargs):
        calls.append((experiment_id, kwargs))
        import shutil
        isolated = _context.experiments_root / "S901" / experiment_id / "artifacts"
        assert isolated != artifacts
        for path in artifacts.iterdir():
            if path.name not in {"external_validation.json", "monitoring_plan.json"}:
                shutil.copyfile(path, isolated / path.name)
        return CommandResult(
            "PASS",
            "strategy.evaluate",
            {
                "recommended_candidate_id": "C001",
                "ranking": {"champion_id": "C001"},
                "machine_evaluation": {
                    "machine_verdict": "ELIGIBLE_FOR_FREEZE_REVIEW",
                    "risk_label": "MIXED",
                    "checks": [
                        {
                            "check_id": "parameter_robustness",
                            "status": "PASS",
                            "reason_codes": [],
                            "metrics": {"valid_neighbor_count": 8},
                        },
                        {
                            "check_id": "statistical_robustness",
                            "status": "PASS",
                            "reason_codes": [],
                            "metrics": {"pbo": 0.4, "dsr_effective_probability": 0.6},
                        },
                        {
                            "check_id": "cost_stress",
                            "status": "PASS",
                            "reason_codes": [],
                            "metrics": {},
                        },
                        {
                            "check_id": "external_reproduction",
                            "status": "NOT_APPLICABLE",
                            "reason_codes": [],
                            "metrics": {},
                        },
                    ],
                },
                "canonical_metric_hash": "e" * 64,
                "input_hash": "f" * 64,
                "decision_hash": "1" * 64,
                "reason_codes": [],
                "formal_evaluation_contract": {
                    "metric_semantics_version": METRIC_SEMANTICS_VERSION,
                    "review_data_hash": kwargs["review_data_hash"],
                    "development_cutoff": mandate.development_cutoff,
                    "windows": mandate.evaluation_windows,
                    "benchmark_id": mandate.benchmark["id"],
                    "primary_fee_rate": mandate.cost_policy["primary_fee_rate"],
                    "stress_scenarios": mandate.cost_policy["stress_scenarios"],
                    "frequency_window_days": mandate.frequency_policy["window_days"],
                    "execution_policy_hash": canonical_sha256(policy),
                    "execution_engine": EXECUTION_CONTRACT_VERSION,
                    "artifact_reuse": False,
                },
            },
        )

    monkeypatch.setattr(
        "czsc_trader.application.freeze_review_service.evaluate_experiment",
        recompute,
    )

    from czsc_trader.application.errors import ValidationError
    source_hashes = {path.name: _hash_file(path) for path in artifacts.iterdir()}
    changed = deepcopy(manifest)
    changed["init_cash"] *= 2
    _write_json(experiment / "candidate_manifest.json", changed)
    with pytest.raises(ValidationError, match="submission seal"):
        evaluate_freeze_review(
            context, "S901", "SGC-S901-001", runtime_root=package,
        )
    assert not calls
    _write_json(experiment / "candidate_manifest.json", manifest)
    result = evaluate_freeze_review(
        context, "S901", "SGC-S901-001", runtime_root=package,
    )
    replay = evaluate_freeze_review(
        context, "S901", "SGC-S901-001", runtime_root=package,
    )
    assert {path.name: _hash_file(path) for path in artifacts.iterdir()} == source_hashes

    assert len(calls) == 1
    experiment_id, kwargs = calls[0]
    assert experiment_id == "0918_TEST"
    assert kwargs["use_cached_result"] is False
    assert kwargs["allow_artifact_reuse"] is False
    assert kwargs["machine_policy"].policy_id == mandate.mandate_id
    assert kwargs["stress_scenarios"] == tuple(mandate.cost_policy["stress_scenarios"])
    assert kwargs["frequency_window_days"] == 60
    assert kwargs["external_replays"] == ()
    assert replay.result["idempotent_replay"] is True
    assert result.result["governance_credential"]["stage"] == "TDR_ADJUDICATED"
    assert result.result["governance_credential"]["result"] == "REJECTED"
    report = result.result["adjudication_report"]
    assert report["machine_verdict"] == "REJECTED"
    assert report["blocking_findings"] == ["CLAIM_MISMATCH"]
    assert report["claim_checks"][0]["status"] == "CLAIM_MISMATCH"
    assert set(report["audit_results"]) == set(audits)
    assert report["audit_results"]["technical_replay"]["mode"] == (
        "FULL_RECOMPUTE_WITHOUT_ARTIFACT_REUSE"
    )
    from czsc_trader.application.freeze_review_service import freeze_review_candidate
    with pytest.raises(ValidationError, match="eligible TDR adjudication"):
        freeze_review_candidate(
            context,
            "S901",
            "SGC-S901-001",
            actor="tester",
            reason="test gate 3",
            change_summary="test",
            runtime_root=package,
        )
    assert registry.versions("S901") == ()
