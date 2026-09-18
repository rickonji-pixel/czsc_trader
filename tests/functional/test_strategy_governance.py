from __future__ import annotations

import json
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
    _load_protocol_and_candidate,
    _runtime_audit,
    _validate_mandate_contract,
    evaluate_freeze_review,
)
from czsc_trader.application.results import CommandResult
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
        _assert_formal_evaluation_contract(stale, mandate, snapshot)


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
    with pytest.raises(ValueError, match="payload execution rules differ"):
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


def test_ft_t05_three_human_gates_create_only_one_frozen_version(
    functional_repo: Path, capsys, monkeypatch
) -> None:
    requirements = [
        {
            "name": "market",
            "dataset": "etf.ohlcv",
            "subject": "588080.SH",
            "frequency": "daily",
            "lookback_sessions": 60,
            "cutoff_rule": "SIGNAL_SESSION",
            "maximum_staleness_days": 0,
        }
    ]
    execution_policy = {
        "policy_type": "FROZEN_RULE",
        "settings": {"buy": "LIMIT", "sell": "MARKET", "fee_rate": 0.001},
    }

    def runtime_pass(version):
        release_hash = version.release_hash or canonical_sha256(version.release_payload())
        return {
            "schema_version": 1,
            "status": "PASS",
            "release_id": version.release_id,
            "release_hash": release_hash,
            "runtime_sha256": "f" * 64,
            "input_contract": {"requirements": requirements},
            "input_contract_sha256": canonical_sha256(requirements),
            "execution_policy": execution_policy,
            "execution_policy_sha256": canonical_sha256(execution_policy),
        }

    monkeypatch.setattr(
        "czsc_trader.application.freeze_review_service.validate_runtime_readiness",
        runtime_pass,
    )
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
        "strategy_payload": {
            "symbol": "588080.SH",
            "target": "position",
            "rule": {"execution": execution_policy["settings"]},
        },
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

    opened = invoke_main(
        [
            "strategy",
            "review",
            "open",
            "--credential",
            "SGC-S900-001",
            "--candidate",
            str(snapshot),
            "--mandate",
            str(mandate),
            "--actor",
            "tester",
            "--reason",
            "批准候选进入冻结流程",
            *root,
        ],
        capsys,
    )
    opened_replay = invoke_main(
        [
            "strategy",
            "review",
            "open",
            "--credential",
            "SGC-S900-001",
            "--candidate",
            str(snapshot),
            "--mandate",
            str(mandate),
            "--actor",
            "tester",
            "--reason",
            "批准候选进入冻结流程",
            *root,
        ],
        capsys,
    )
    registry = StrategyRegistry(functional_repo / "strategies")
    credential = registry.get_governance_credential("S900", "SGC-S900-001")
    submission = credential.seals[-1]
    stored_snapshot = CandidateSnapshot.from_dict(submission.content["candidate_snapshot"])
    stored_mandate = EvaluationMandate.from_dict(submission.content["evaluation_mandate"])
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
        "audit_results": {
            name: {"status": "PASS", "evidence_hash": "d" * 64}
            for name in stored_mandate.required_audits
        },
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
        artifact_hashes={"adjudication_report": report.report_hash},
    )
    frozen = invoke_main(
        [
            "strategy",
            "freeze",
            "--strategy",
            "S900",
            "--credential",
            "SGC-S900-001",
            "--change-summary",
            "首个冻结版本",
            "--actor",
            "tester",
            "--reason",
            "批准低成本模拟观察",
            *root,
        ],
        capsys,
    )
    replay = invoke_main(
        [
            "strategy",
            "freeze",
            "--strategy",
            "S900",
            "--credential",
            "SGC-S900-001",
            "--change-summary",
            "首个冻结版本",
            "--actor",
            "tester",
            "--reason",
            "批准低成本模拟观察",
            *root,
        ],
        capsys,
    )

    assert created["result"]["family"]["strategy_id"] == "S900"
    assert opened["result"]["governance_credential"]["stage"] == "CANDIDATE_SUBMITTED"
    assert len(opened_replay["result"]["governance_credential"]["seals"]) == 2
    assert frozen["result"]["version"]["release_id"] == "S900-v1"
    assert frozen["result"]["pte_deployment"] == "NOT_REQUESTED"
    assert replay["result"]["idempotent_replay"] is True
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
    assert registry.validate_all()["credentials"] == 1
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


def test_ft_t06_tdr_recomputes_every_required_audit_without_cached_evidence(
    functional_repo: Path, monkeypatch
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
    requirements = [
        {
            "name": "market",
            "dataset": "etf.ohlcv",
            "subject": "588080.SH",
            "frequency": "daily",
            "lookback_sessions": 60,
            "cutoff_rule": "SIGNAL_SESSION",
            "maximum_staleness_days": 0,
        }
    ]
    policy = {
        "policy_type": "FROZEN_RULE",
        "settings": {"buy": "LIMIT", "sell": "MARKET", "fee_rate": 0.001},
    }
    snapshot_raw = {
        "schema_version": 1,
        "strategy_id": "S901",
        "candidate_id": "C001",
        "source_experiment": "experiments/S901/0918_TEST",
        "strategy_payload": {
            "symbol": "588080.SH",
            "target": "position",
            "rule": {"execution": policy["settings"]},
        },
        "data_contract": {
            "symbol": "588080.SH",
            "asset_type": "etf",
            "requirements": requirements,
        },
        "execution_policy": policy,
        "research_claims": {"annual_return": 0.20, "maximum_drawdown": -0.18},
    }
    snapshot = CandidateSnapshot.from_dict(_hashed(snapshot_raw, "candidate_hash"))
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
    registry.append_governance_seal(
        "S901",
        "SGC-S901-001",
        stage=GovernanceStage.CANDIDATE_SUBMITTED,
        result=GovernanceResult.OPEN,
        actor="tester",
        expected_previous_hash=credential.credential_hash,
        content={
            "submission_id": "SUB-SGC-S901-001-2",
            "candidate_snapshot": snapshot.to_dict(),
            "evaluation_mandate": mandate.to_dict(),
            "audit_policy": audit_policy,
        },
        artifact_hashes={
            "candidate_snapshot": canonical_sha256(snapshot.to_dict()),
            "evaluation_mandate": mandate.mandate_hash,
            "audit_policy": canonical_sha256(audit_policy),
        },
    )

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

    calls = []

    def recompute(_context, experiment_id, **kwargs):
        calls.append((experiment_id, kwargs))
        return CommandResult(
            "PASS",
            "strategy.evaluate",
            {
                "recommended_candidate_id": "C001",
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
    monkeypatch.setattr(
        "czsc_trader.application.freeze_review_service.validate_runtime_readiness",
        lambda version: {
            "schema_version": 1,
            "status": "PASS",
            "release_id": version.release_id,
            "release_hash": canonical_sha256(version.release_payload()),
            "runtime_sha256": "2" * 64,
            "input_contract": {"requirements": requirements},
            "input_contract_sha256": canonical_sha256(requirements),
            "execution_policy": policy,
            "execution_policy_sha256": canonical_sha256(policy),
        },
    )

    result = evaluate_freeze_review(context, "S901", "SGC-S901-001")
    replay = evaluate_freeze_review(context, "S901", "SGC-S901-001")

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
    assert result.result["governance_credential"]["result"] == "ELIGIBLE"
    report = result.result["adjudication_report"]
    assert report["machine_verdict"] == "ELIGIBLE_FOR_FREEZE_REVIEW"
    assert set(report["audit_results"]) == set(audits)
    assert report["audit_results"]["technical_replay"]["mode"] == (
        "FULL_RECOMPUTE_WITHOUT_ARTIFACT_REUSE"
    )
