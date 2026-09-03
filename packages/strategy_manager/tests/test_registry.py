import json

import pytest

from strategy_manager import (
    ImmutableVersionError,
    Qualification,
    RegistryError,
    Strategy,
    StrategyRegistry,
    StrategyVersion,
)

from conftest import make_evidence, seed_research_registry


def test_create_and_resolve_strategy_version(tmp_path, strategy, research_version):
    registry = seed_research_registry(tmp_path, strategy, research_version)

    assert registry.get_strategy("S001") == strategy
    assert registry.get_version("S001", "v1") == research_version
    assert registry.resolve_strategy("S001").release_id == "S001-v1"
    assert registry.current_qualification("S001", "v1") is Qualification.RESEARCH


def test_registry_rejects_duplicate_normalized_active_names(tmp_path, strategy):
    registry = StrategyRegistry(tmp_path)
    registry.create_strategy(strategy, actor="tester", reason="创建策略")
    duplicate = Strategy.from_dict(
        {**strategy.to_dict(), "strategy_id": "S002", "name": "  综合基线策略  "}
    )

    with pytest.raises(RegistryError, match="name"):
        registry.create_strategy(duplicate, actor="tester", reason="重复名称")


def test_versions_are_consecutive_and_point_to_parent(tmp_path, strategy, research_version):
    registry = seed_research_registry(tmp_path, strategy, research_version)
    skipped = StrategyVersion.from_dict(
        {
            **research_version.to_dict(),
            "version": "v3",
            "release_id": "S001-v3",
            "parent_version": "v1",
        }
    )

    with pytest.raises(RegistryError, match="next version"):
        registry.create_version(skipped, actor="tester", reason="跳号")


def test_freeze_hashes_version_and_detects_later_tampering(
    tmp_path, strategy, research_version
):
    registry = seed_research_registry(tmp_path, strategy, research_version)
    evidence = make_evidence(
        evidence_id="EVD-20260903-0001",
        phase="RESEARCH_BACKTEST",
        release_hash="a" * 64,
    )

    frozen, event = registry.freeze_version(
        "S001", "v1", actor="tester", reason="进入模拟盘", evidence=evidence
    )

    assert frozen.release_hash
    assert event.to_state is Qualification.PAPER_READY
    assert event.evidence_ids == ["EVD-20260903-0001"]
    assert registry.evidence("S001")[0].release_hash == frozen.release_hash

    version_path = tmp_path / "S001" / "versions" / "v1.json"
    payload = json.loads(version_path.read_text(encoding="utf-8"))
    payload["strategy_payload"]["fee_rate"] = 0.01
    version_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ImmutableVersionError, match="release hash"):
        registry.validate_all()
