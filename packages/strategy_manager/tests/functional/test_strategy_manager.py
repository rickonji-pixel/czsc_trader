from __future__ import annotations

import json
from pathlib import Path

import pytest

from strategy_manager import (
    EvidenceRequiredError,
    ImmutableVersionError,
    InvalidTransitionError,
    PerformanceEvidence,
    Qualification,
    RegistryError,
    Strategy,
    StrategyRegistry,
    StrategyVersion,
    ValidationError,
)


def _strategy(strategy_id: str = "S001", name: str = "综合基线策略") -> Strategy:
    return Strategy.from_dict(
        {
            "schema_version": 1,
            "strategy_id": strategy_id,
            "name": name,
            "objective": "验证策略治理链路",
            "responsibility": "生成目标仓位",
            "scope": ["588080.SH"],
            "created_at": "2026-09-03T10:00:00+08:00",
            "created_by": "tester",
        }
    )


def _version(
    strategy_id: str = "S001",
    version: str = "v1",
    parent: str | None = None,
) -> StrategyVersion:
    return StrategyVersion.from_dict(
        {
            "schema_version": 1,
            "strategy_id": strategy_id,
            "version": version,
            "release_id": f"{strategy_id}-{version}",
            "parent_version": parent,
            "change_summary": "功能测试版本",
            "source_experiment": "experiments/0904_TEST",
            "source_candidate": "winner",
            "selection_data_cutoff": "2026-09-02",
            "forward_start": "2026-09-03",
            "strategy_payload": {"symbol": "588080.SH", "fee_rate": 0.0005},
            "release_hash": None,
        }
    )


def _evidence(
    evidence_id: str,
    phase: str,
    release_hash: str,
) -> PerformanceEvidence:
    return PerformanceEvidence.from_dict(
        {
            "schema_version": 1,
            "evidence_id": evidence_id,
            "strategy_id": "S001",
            "version": "v1",
            "release_hash": release_hash,
            "phase": phase,
            "period_start": "2021-01-01",
            "period_end": "2026-09-02",
            "data_identity": {"symbol": "588080.SH", "cutoff": "2026-09-02"},
            "initial_capital": 100000.0,
            "fee_rate": 0.0005,
            "maximum_drawdown": -0.12,
            "calmar_ratio": 1.4,
            "win_loss_ratio": 1.2,
            "win_loss_ratio_status": "VALID",
            "total_return": 0.35,
            "sharpe_ratio": 0.8,
            "closed_trades": 22,
            "source_path": "functional://evidence",
            "source_hash": "b" * 64,
            "recorded_at": "2026-09-03T10:00:00+08:00",
            "recorded_by": "tester",
        }
    )


def _approval(*, decision: str = "RECOMMEND_FREEZE") -> dict:
    return {
        "schema_version": 1,
        "assessment_id": "FHC-S001-v1",
        "strategy_id": "S001",
        "candidate_id": "winner",
        "candidate_hash": "c" * 64,
        "decision": decision,
        "risk_label": "MIXED",
        "source_experiment": "experiments/0904_TEST",
        "source_hash": "d" * 64,
        "assessed_at": "2026-09-03T10:00:00+08:00",
    }


def _frozen_registry(root: Path) -> tuple[StrategyRegistry, str]:
    registry = StrategyRegistry(root)
    registry.create_strategy(_strategy(), actor="tester", reason="创建策略")
    registry.create_version(_version(), actor="tester", reason="创建首版")
    frozen, _event = registry.freeze_version(
        "S001",
        "v1",
        actor="tester",
        reason="进入模拟盘",
        evidence=_evidence("EVD-RESEARCH", "RESEARCH_BACKTEST", "a" * 64),
        approval=_approval(),
    )
    assert frozen.release_hash is not None
    return registry, frozen.release_hash


def test_ft_sm01_complete_lifecycle_is_persistent_and_auditable(
    tmp_path: Path,
) -> None:
    registry, release_hash = _frozen_registry(tmp_path)
    paper = _evidence("EVD-PAPER", "PAPER_FORWARD", release_hash)
    registry.record_evidence(paper)
    promoted = registry.promote_version(
        "S001",
        "v1",
        actor="tester",
        reason="模拟盘证据通过",
        evidence_ids=[paper.evidence_id],
    )
    assert promoted.to_state is Qualification.LIVE_READY
    assert registry.assert_deployable("S001", "v1", "LIVE").release_hash == release_hash
    downgraded = registry.downgrade_version(
        "S001",
        "v1",
        actor="tester",
        reason="降回模拟盘",
        evidence_ids=[paper.evidence_id],
    )
    retired = registry.retire_version(
        "S001", "v1", actor="tester", reason="停止部署"
    )

    reopened = StrategyRegistry(tmp_path)
    assert reopened.get_strategy("S001") == _strategy()
    assert reopened.get_version("S001", "v1").release_hash == release_hash
    assert reopened.resolve_strategy("S001").release_id == "S001-v1"
    assert reopened.current_qualification("S001", "v1") is Qualification.RETIRED
    assert downgraded.to_state is Qualification.PAPER_READY
    assert retired.to_state is Qualification.RETIRED
    assert [event.event_type for event in reopened.lifecycle_events("S001")] == [
        "VERSION_CREATED",
        "VERSION_FROZEN",
        "VERSION_PROMOTED",
        "VERSION_DOWNGRADED",
        "VERSION_RETIRED",
    ]
    assert [item.evidence_id for item in reopened.evidence("S001", "v1")] == [
        "EVD-RESEARCH",
        "EVD-PAPER",
    ]
    with pytest.raises(InvalidTransitionError, match="deployable"):
        reopened.assert_deployable("S001", "v1", "PAPER")


def test_ft_sm02_governance_rejects_invalid_mutation_and_tampering(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="unknown fields"):
        Strategy.from_dict({**_strategy().to_dict(), "unexpected": True})
    with pytest.raises(ValidationError, match="strategy_id"):
        Strategy.from_dict({**_strategy().to_dict(), "strategy_id": "baseline-143"})

    registry = StrategyRegistry(tmp_path / "rules")
    registry.create_strategy(_strategy(), actor="tester", reason="创建策略")
    with pytest.raises(RegistryError, match="name"):
        registry.create_strategy(
            _strategy("S002", "  综合基线策略  "),
            actor="tester",
            reason="重复名称",
        )
    registry.create_version(_version(), actor="tester", reason="创建首版")
    with pytest.raises(ValidationError, match="RECOMMEND_FREEZE"):
        registry.freeze_version(
            "S001",
            "v1",
            actor="tester",
            reason="体检未通过",
            evidence=_evidence("EVD-REJECTED", "RESEARCH_BACKTEST", "a" * 64),
            approval=_approval(decision="KEEP_RESEARCHING"),
        )
    with pytest.raises(RegistryError, match="next version"):
        registry.create_version(
            _version(version="v3", parent="v1"),
            actor="tester",
            reason="跳过版本",
        )
    frozen, _event = registry.freeze_version(
        "S001",
        "v1",
        actor="tester",
        reason="进入模拟盘",
        evidence=_evidence("EVD-RESEARCH", "RESEARCH_BACKTEST", "a" * 64),
        approval=_approval(),
    )
    before = registry.lifecycle_events("S001")
    with pytest.raises(EvidenceRequiredError, match="PAPER_FORWARD"):
        registry.promote_version(
            "S001", "v1", actor="tester", reason="证据不足", evidence_ids=[]
        )
    assert registry.lifecycle_events("S001") == before
    registry.retire_version("S001", "v1", actor="tester", reason="停止部署")
    with pytest.raises(InvalidTransitionError):
        registry.promote_version(
            "S001", "v1", actor="tester", reason="尝试恢复", evidence_ids=[]
        )

    version_path = tmp_path / "rules" / "S001" / "versions" / "v1.json"
    payload = json.loads(version_path.read_text(encoding="utf-8"))
    payload["strategy_payload"]["fee_rate"] = 0.01
    version_path.write_text(json.dumps(payload), encoding="utf-8")
    assert frozen.release_hash is not None
    with pytest.raises(ImmutableVersionError, match="release hash"):
        registry.validate_all()
