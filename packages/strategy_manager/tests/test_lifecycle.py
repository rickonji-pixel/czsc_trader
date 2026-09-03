import pytest

from strategy_manager import (
    EvidenceRequiredError,
    InvalidTransitionError,
    Qualification,
)

from conftest import make_evidence, seed_research_registry


def _paper_registry(tmp_path, strategy, research_version):
    registry = seed_research_registry(tmp_path, strategy, research_version)
    evidence = make_evidence(
        evidence_id="EVD-20260903-0001",
        phase="RESEARCH_BACKTEST",
        release_hash="a" * 64,
    )
    frozen, _ = registry.freeze_version(
        "S001", "v1", actor="tester", reason="进入模拟盘", evidence=evidence
    )
    return registry, frozen


def test_failed_promotion_leaves_event_history_unchanged(tmp_path, strategy, research_version):
    registry, _ = _paper_registry(tmp_path, strategy, research_version)
    before = registry.lifecycle_events("S001")

    with pytest.raises(EvidenceRequiredError, match="PAPER_FORWARD"):
        registry.promote_version(
            "S001", "v1", actor="tester", reason="证据不足", evidence_ids=[]
        )

    assert registry.lifecycle_events("S001") == before


def test_paper_evidence_allows_promotion_and_live_deployment(
    tmp_path, strategy, research_version
):
    registry, frozen = _paper_registry(tmp_path, strategy, research_version)
    paper = make_evidence(
        evidence_id="EVD-20261203-0001",
        phase="PAPER_FORWARD",
        release_hash=frozen.release_hash,
        source_path="paper-evidence.json",
    )
    registry.record_evidence(paper)

    event = registry.promote_version(
        "S001",
        "v1",
        actor="tester",
        reason="模拟盘证据通过人工评审",
        evidence_ids=[paper.evidence_id],
    )

    assert event.to_state is Qualification.LIVE_READY
    assert registry.assert_deployable("S001", "v1", "LIVE") == frozen


def test_retired_version_cannot_return_to_service(tmp_path, strategy, research_version):
    registry, _ = _paper_registry(tmp_path, strategy, research_version)
    registry.retire_version("S001", "v1", actor="tester", reason="停止部署")

    with pytest.raises(InvalidTransitionError):
        registry.promote_version(
            "S001", "v1", actor="tester", reason="尝试恢复", evidence_ids=[]
        )
    with pytest.raises(InvalidTransitionError, match="deployable"):
        registry.assert_deployable("S001", "v1", "PAPER")
