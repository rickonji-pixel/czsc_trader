from __future__ import annotations

import json
from pathlib import Path

import pytest

from strategy_manager import (
    AdjudicationReport,
    CandidateSnapshot,
    EvaluationMandate,
    EvidenceRequiredError,
    ImmutableVersionError,
    InvalidTransitionError,
    PerformanceEvidence,
    Qualification,
    RegistryError,
    ReviewStatus,
    StrategyFamily,
    StrategyRegistry,
    ValidationError,
    canonical_sha256,
)


def _hashed(payload: dict, field: str) -> dict:
    value = dict(payload)
    value[field] = canonical_sha256(payload)
    return value


def _family(strategy_id: str = "S008", name: str = "治理功能测试策略") -> StrategyFamily:
    return StrategyFamily.from_dict(
        {
            "schema_version": 2,
            "strategy_id": strategy_id,
            "name": name,
            "scope": ["588080.SH"],
            "research_intent": {
                "objective": "验证可信策略治理链路",
                "responsibility": "研究并生成目标仓位",
            },
            "research_state": "RESEARCHING",
            "created_at": "2026-09-18T10:00:00+08:00",
            "created_by": "tester",
            "updated_at": "2026-09-18T10:00:00+08:00",
        }
    )


def _candidate(strategy_id: str = "S008") -> CandidateSnapshot:
    payload = {
        "schema_version": 1,
        "strategy_id": strategy_id,
        "candidate_id": "C001",
        "source_experiment": "experiments/S008/0918_EX01",
        "strategy_payload": {
            "symbol": "588080.SH",
            "runtime": {
                "module": "strategies.s007_v1",
                "class": "S007V1Strategy",
            },
        },
        "data_contract": {"subject": "588080.SH", "required_history": 60},
        "execution_policy": {"buy": "LIMIT", "sell": "MARKET", "fee_rate": 0.001},
        "research_claims": {"annual_return": 0.20, "maximum_drawdown": -0.18},
    }
    return CandidateSnapshot.from_dict(_hashed(payload, "candidate_hash"))


def _mandate(strategy_id: str = "S008") -> EvaluationMandate:
    payload = {
        "schema_version": 1,
        "mandate_id": "EM-S008-C001-001",
        "strategy_id": strategy_id,
        "candidate_id": "C001",
        "development_cutoff": "2026-09-02",
        "forward_start": "2026-09-03",
        "evaluation_windows": {
            "full": {"start": "2021-01-01", "end": "2026-09-02"}
        },
        "benchmark": {"type": "strategy", "id": "S001-v2"},
        "objectives": [
            {"metric": "annual_return", "operator": ">=", "value": 0.15},
            {"metric": "maximum_drawdown", "operator": ">=", "value": -0.20},
        ],
        "cost_policy": {"primary_fee_rate": 0.001, "stress_fee_rate": 0.0015},
        "frequency_policy": {"mode": "OBSERVE", "window_days": 60},
        "required_audits": [
            "objective_recalculation",
            "parameter_robustness",
            "statistical_robustness",
            "cost_stress",
            "runtime_acceptance",
        ],
        "evidence_seen_through": "2026-09-02",
        "finalized_at": "2026-09-18T11:00:00+08:00",
        "finalized_by": "tester",
    }
    return EvaluationMandate.from_dict(_hashed(payload, "mandate_hash"))


def _report(
    registry: StrategyRegistry,
    *,
    strategy_id: str = "S008",
    review_id: str = "FR-S008-C001-001",
    missing_audit: str | None = None,
    verdict: str = "ELIGIBLE_FOR_FREEZE_REVIEW",
) -> AdjudicationReport:
    case, snapshot, mandate = registry.get_freeze_review(strategy_id, review_id)
    audits = {
        name: {"status": "PASS", "evidence_hash": canonical_sha256({"audit": name})}
        for name in mandate.required_audits
        if name != missing_audit
    }
    payload = {
        "schema_version": 1,
        "report_id": f"ADR-{review_id}",
        "review_id": review_id,
        "strategy_id": strategy_id,
        "candidate_id": snapshot.candidate_id,
        "candidate_hash": snapshot.candidate_hash,
        "evaluation_mandate_hash": mandate.mandate_hash,
        "audit_policy_hash": case.audit_policy_hash,
        "claim_checks": [
            {
                "claim": "annual_return",
                "claimed": 0.20,
                "recalculated": 0.201,
                "status": "PASS",
            }
        ],
        "audit_results": audits,
        "machine_verdict": verdict,
        "risk_label": "MIXED",
        "blocking_findings": [] if verdict != "INCOMPLETE" else ["MISSING_AUDIT"],
        "reservations": ["统计显著性仍需前瞻样本"],
        "generated_at": "2026-09-18T12:00:00+08:00",
    }
    return AdjudicationReport.from_dict(_hashed(payload, "report_hash"))


def _evidence(strategy_id: str = "S008") -> dict:
    return {
        "schema_version": 1,
        "evidence_id": "EVD-S008-C001-RESEARCH",
        "strategy_id": strategy_id,
        "version": "v1",
        "release_hash": "a" * 64,
        "phase": "RESEARCH_BACKTEST",
        "period_start": "2021-01-01",
        "period_end": "2026-09-02",
        "data_identity": {"symbol": "588080.SH", "cutoff": "2026-09-02"},
        "initial_capital": 100000.0,
        "fee_rate": 0.001,
        "maximum_drawdown": -0.18,
        "calmar_ratio": 1.2,
        "win_loss_ratio": 1.3,
        "win_loss_ratio_status": "VALID",
        "total_return": 0.42,
        "sharpe_ratio": 0.9,
        "closed_trades": 35,
        "source_path": "experiments/S008/0918_EX01/artifacts/backtest.json",
        "source_hash": "b" * 64,
        "recorded_at": "2026-09-18T12:00:00+08:00",
        "recorded_by": "tdr",
    }


def _decision(candidate: CandidateSnapshot) -> dict:
    payload = {
        "schema_version": 1,
        "review_id": "FR-S008-C001-001",
        "strategy_id": "S008",
        "candidate_id": candidate.candidate_id,
        "candidate_hash": candidate.candidate_hash,
        "decision": "APPROVE_FREEZE",
        "actor": "owner",
        "reason": "批准低成本模拟观察",
        "decided_at": "2026-09-18T13:00:00+08:00",
    }
    return _hashed(payload, "decision_hash")


def _runtime(candidate: CandidateSnapshot) -> dict:
    release_hash = canonical_sha256(
        {
            "schema_version": 2,
            "strategy_id": "S008",
            "version": "v1",
            "release_id": "S008-v1",
            "strategy_payload": candidate.strategy_payload,
        }
    )
    return {
        "schema_version": 1,
        "status": "PASS",
        "release_id": "S008-v1",
        "release_hash": release_hash,
        "strategy_payload_hash": canonical_sha256(candidate.strategy_payload),
        "runtime_sha256": "f" * 64,
    }


def _ready_registry(root: Path) -> tuple[StrategyRegistry, CandidateSnapshot]:
    registry = StrategyRegistry(root)
    registry.create_family(_family(), actor="owner", reason="批准立项")
    candidate = _candidate()
    registry.open_freeze_review(
        "FR-S008-C001-001", candidate, _mandate(), actor="owner"
    )
    registry.record_adjudication(_report(registry))
    return registry, candidate


def test_ft_sm01_three_gate_governance_is_persistent_and_auditable(tmp_path: Path) -> None:
    registry, candidate = _ready_registry(tmp_path)
    frozen, event = registry.create_frozen_version(
        "S008",
        "FR-S008-C001-001",
        actor="owner",
        reason="批准低成本模拟观察",
        human_decision=_decision(candidate),
        runtime_acceptance=_runtime(candidate),
        evidence=_evidence(),
        change_summary="首个冻结版本",
    )

    reopened = StrategyRegistry(tmp_path)
    case, snapshot, mandate = reopened.get_freeze_review(
        "S008", "FR-S008-C001-001"
    )
    assert case.status is ReviewStatus.FROZEN
    assert snapshot.candidate_hash == candidate.candidate_hash
    assert mandate.mandate_hash == frozen.governance["evaluation_mandate_hash"]
    assert frozen.schema_version == 2
    assert frozen.version == "v1"
    assert frozen.release_hash is not None
    assert event.event_type == "VERSION_FROZEN"
    assert reopened.current_qualification("S008", "v1") is Qualification.PAPER_READY
    assert reopened.assert_deployable("S008", "v1", "PAPER") == frozen
    assert [item.evidence_id for item in reopened.evidence("S008", "v1")] == [
        "EVD-S008-C001-RESEARCH"
    ]


def test_ft_sm02_missing_audit_and_hash_drift_cannot_freeze(tmp_path: Path) -> None:
    registry = StrategyRegistry(tmp_path)
    registry.create_family(_family(), actor="owner", reason="批准立项")
    candidate = _candidate()
    registry.open_freeze_review(
        "FR-S008-C001-001", candidate, _mandate(), actor="owner"
    )
    with pytest.raises(RegistryError, match="omits required audits"):
        registry.record_adjudication(
            _report(registry, missing_audit="cost_stress")
        )

    incomplete = _report(
        registry,
        missing_audit="cost_stress",
        verdict="INCOMPLETE",
    )
    case = registry.record_adjudication(incomplete)
    assert case.status is ReviewStatus.INCOMPLETE
    with pytest.raises(EvidenceRequiredError, match="not eligible"):
        registry.create_frozen_version(
            "S008",
            "FR-S008-C001-001",
            actor="owner",
            reason="批准低成本模拟观察",
            human_decision=_decision(candidate),
            runtime_acceptance={"status": "PASS"},
            evidence=_evidence(),
            change_summary="不得冻结",
        )

    snapshot_path = (
        tmp_path
        / "S008"
        / "reviews"
        / "FR-S008-C001-001"
        / "candidate_snapshot.json"
    )
    raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
    raw["strategy_payload"]["tampered"] = True
    snapshot_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises((RegistryError, ValidationError), match="hash"):
        registry.get_freeze_review("S008", "FR-S008-C001-001")


def test_ft_sm03_family_and_mandate_contracts_reject_false_success() -> None:
    with pytest.raises(ValidationError, match="unknown fields"):
        StrategyFamily.from_dict({**_family().to_dict(), "unexpected": True})
    with pytest.raises(ValidationError, match="strategy_id"):
        StrategyFamily.from_dict(
            {**_family().to_dict(), "strategy_id": "baseline-143"}
        )
    raw = _mandate().to_dict()
    raw["evidence_seen_through"] = "2026-09-03"
    payload = dict(raw)
    payload.pop("mandate_hash")
    raw["mandate_hash"] = canonical_sha256(payload)
    with pytest.raises(ValidationError, match="evidence_seen_through"):
        EvaluationMandate.from_dict(raw)


def test_ft_sm04_existing_frozen_version_remains_hash_protected(tmp_path: Path) -> None:
    registry, candidate = _ready_registry(tmp_path)
    frozen, _ = registry.create_frozen_version(
        "S008",
        "FR-S008-C001-001",
        actor="owner",
        reason="批准低成本模拟观察",
        human_decision=_decision(candidate),
        runtime_acceptance=_runtime(candidate),
        evidence=_evidence(),
        change_summary="首个冻结版本",
    )
    path = tmp_path / "S008" / "versions" / "v1.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["strategy_payload"]["tampered"] = True
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert frozen.release_hash is not None
    with pytest.raises(ImmutableVersionError, match="release hash"):
        registry.validate_all()


def test_ft_sm04b_governance_hash_and_runtime_release_are_independently_protected(
    tmp_path: Path,
) -> None:
    registry, candidate = _ready_registry(tmp_path)
    mismatched_runtime = _runtime(candidate)
    mismatched_runtime["release_hash"] = "0" * 64
    with pytest.raises(EvidenceRequiredError, match="release hash differs"):
        registry.create_frozen_version(
            "S008",
            "FR-S008-C001-001",
            actor="owner",
            reason="批准低成本模拟观察",
            human_decision=_decision(candidate),
            runtime_acceptance=mismatched_runtime,
            evidence=_evidence(),
            change_summary="首个冻结版本",
        )

    frozen, _ = registry.create_frozen_version(
        "S008",
        "FR-S008-C001-001",
        actor="owner",
        reason="批准低成本模拟观察",
        human_decision=_decision(candidate),
        runtime_acceptance=_runtime(candidate),
        evidence=_evidence(),
        change_summary="首个冻结版本",
    )
    path = tmp_path / "S008" / "versions" / "v1.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["governance"]["adjudication_report_hash"] = "0" * 64
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert frozen.release_hash == _runtime(candidate)["release_hash"]
    with pytest.raises(RegistryError, match="governance_hash"):
        registry.validate_all()


def test_ft_sm04c_failed_freeze_commit_leaves_no_partial_version(
    tmp_path: Path, monkeypatch
) -> None:
    registry, candidate = _ready_registry(tmp_path)
    original = registry._atomic_write

    def fail_runtime_write(path: Path, text: str, expected: bytes | None = None) -> None:
        if path.name == "runtime_acceptance.json":
            raise OSError("injected runtime acceptance write failure")
        original(path, text, expected)

    monkeypatch.setattr(registry, "_atomic_write", fail_runtime_write)
    with pytest.raises(OSError, match="injected"):
        registry.create_frozen_version(
            "S008",
            "FR-S008-C001-001",
            actor="owner",
            reason="批准低成本模拟观察",
            human_decision=_decision(candidate),
            runtime_acceptance=_runtime(candidate),
            evidence=_evidence(),
            change_summary="首个冻结版本",
        )

    clean = StrategyRegistry(tmp_path)
    case, _snapshot, _mandate_value = clean.get_freeze_review(
        "S008", "FR-S008-C001-001"
    )
    assert case.status is ReviewStatus.ELIGIBLE
    assert clean.versions("S008") == ()
    assert clean.evidence("S008") == []
    assert [item.event_type for item in clean.lifecycle_events("S008")] == [
        "RESEARCH_BATCH_CREATED"
    ]
    review_dir = tmp_path / "S008" / "reviews" / "FR-S008-C001-001"
    assert not (review_dir / "human_decision.json").exists()
    assert not (review_dir / "runtime_acceptance.json").exists()


def test_ft_sm05_legacy_governance_event_is_idempotent(tmp_path: Path) -> None:
    registry = StrategyRegistry(tmp_path)
    registry.create_family(_family(), actor="owner", reason="迁移策略族")
    from strategy_manager import FreezeApproval, StrategyVersion

    version = StrategyVersion.from_dict(
        {
            "schema_version": 1,
            "strategy_id": "S008",
            "version": "v1",
            "release_id": "S008-v1",
            "parent_version": None,
            "change_summary": "历史冻结版本",
            "source_experiment": "experiments/legacy",
            "source_candidate": "legacy",
            "selection_data_cutoff": "2026-09-02",
            "forward_start": "2026-09-03",
            "strategy_payload": {"symbol": "588080.SH"},
            "release_hash": None,
        }
    )
    registry.create_version(version, actor="migration", reason="历史迁移")
    report_payload = {
        "schema_version": 2,
        "report_id": "SE-LEGACY",
        "candidate_id": "legacy",
        "candidate_hash": "c" * 64,
        "machine_verdict": "ELIGIBLE_FOR_FREEZE_REVIEW",
        "risk_label": "MIXED",
    }
    machine_report = {
        **report_payload,
        "report_hash": canonical_sha256(report_payload),
    }
    approval = FreezeApproval.from_dict(
        {
            "schema_version": 2,
            "assessment_id": "FR-LEGACY",
            "strategy_id": "S008",
            "candidate_id": "legacy",
            "candidate_hash": "c" * 64,
            "decision": "APPROVE_FREEZE",
            "risk_label": "MIXED",
            "source_experiment": "experiments/legacy",
            "machine_report_id": "SE-LEGACY",
            "machine_report_hash": machine_report["report_hash"],
            "machine_verdict": "ELIGIBLE_FOR_FREEZE_REVIEW",
            "reviewed_by": "migration",
            "rationale": "保留历史事实",
            "mechanism_review": "APPROVED",
            "external_relevance_review": "APPROVED",
            "deployment_review": "APPROVED",
            "monitoring_plan_review": "APPROVED",
            "reviewed_at": "2026-09-18T10:00:00+08:00",
        }
    )
    registry.freeze_version(
        "S008",
        "v1",
        actor="migration",
        reason="历史冻结",
        evidence=_evidence(),
        approval=approval,
        machine_report=machine_report,
    )
    first = registry.record_legacy_governance_acceptance(
        "S008",
        "v1",
        actor="migration",
        reason="接受历史治理事实",
        cutover_commit="e509b55",
    )
    second = registry.record_legacy_governance_acceptance(
        "S008",
        "v1",
        actor="migration",
        reason="接受历史治理事实",
        cutover_commit="e509b55",
    )
    assert first == second
    assert registry.current_qualification("S008", "v1") is Qualification.PAPER_READY
    assert [item.event_type for item in registry.lifecycle_events("S008")].count(
        "LEGACY_GOVERNANCE_ACCEPTED"
    ) == 1


def test_ft_sm06_lifecycle_still_requires_forward_evidence(tmp_path: Path) -> None:
    registry, candidate = _ready_registry(tmp_path)
    frozen, _ = registry.create_frozen_version(
        "S008",
        "FR-S008-C001-001",
        actor="owner",
        reason="批准低成本模拟观察",
        human_decision=_decision(candidate),
        runtime_acceptance=_runtime(candidate),
        evidence=_evidence(),
        change_summary="首个冻结版本",
    )
    with pytest.raises(EvidenceRequiredError, match="PAPER_FORWARD"):
        registry.promote_version(
            "S008", "v1", actor="owner", reason="证据不足", evidence_ids=[]
        )
    paper_raw = _evidence()
    paper_raw.update(
        {
            "evidence_id": "EVD-S008-PAPER",
            "release_hash": frozen.release_hash,
            "phase": "PAPER_FORWARD",
            "period_start": "2026-09-03",
            "period_end": "2026-09-18",
        }
    )
    registry.record_evidence(PerformanceEvidence.from_dict(paper_raw))
    registry.promote_version(
        "S008",
        "v1",
        actor="owner",
        reason="模拟盘证据通过",
        evidence_ids=["EVD-S008-PAPER"],
    )
    registry.retire_version("S008", "v1", actor="owner", reason="停止部署")
    with pytest.raises(InvalidTransitionError):
        registry.promote_version(
            "S008", "v1", actor="owner", reason="不得恢复", evidence_ids=[]
        )
