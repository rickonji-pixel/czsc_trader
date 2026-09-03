from pathlib import Path

import pytest

from strategy_manager import PerformanceEvidence, Strategy, StrategyVersion


@pytest.fixture
def strategy() -> Strategy:
    return Strategy.from_dict(
        {
            "schema_version": 1,
            "strategy_id": "S001",
            "name": "综合基线策略",
            "objective": "验证策略治理链路",
            "responsibility": "生成目标仓位",
            "scope": ["588080.SH"],
            "created_at": "2026-09-03T10:00:00+08:00",
            "created_by": "tester",
        }
    )


@pytest.fixture
def research_version() -> StrategyVersion:
    return StrategyVersion.from_dict(
        {
            "schema_version": 1,
            "strategy_id": "S001",
            "version": "v1",
            "release_id": "S001-v1",
            "parent_version": None,
            "change_summary": "首个版本",
            "source_experiment": "experiments/0901_EX20",
            "source_candidate": 143,
            "selection_data_cutoff": "2026-09-02",
            "forward_start": "2026-09-03",
            "strategy_payload": {"symbol": "588080.SH", "fee_rate": 0.0003},
            "release_hash": None,
        }
    )


def make_evidence(
    *,
    evidence_id: str,
    phase: str,
    release_hash: str,
    source_path: str = "experiments/0901_EX20/results.json",
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
            "period_end": "2025-12-31",
            "data_identity": {"symbol": "588080.SH", "cutoff": "2026-09-02"},
            "initial_capital": 100000.0,
            "fee_rate": 0.0003,
            "maximum_drawdown": -0.12,
            "calmar_ratio": 1.4,
            "win_loss_ratio": 1.2,
            "win_loss_ratio_status": "VALID",
            "total_return": 0.35,
            "sharpe_ratio": 0.8,
            "closed_trades": 22,
            "source_path": source_path,
            "source_hash": "b" * 64,
            "recorded_at": "2026-09-03T10:00:00+08:00",
            "recorded_by": "tester",
        }
    )


def seed_research_registry(root: Path, strategy: Strategy, version: StrategyVersion):
    from strategy_manager import StrategyRegistry

    registry = StrategyRegistry(root)
    registry.create_strategy(strategy, actor="tester", reason="创建策略")
    registry.create_version(version, actor="tester", reason="创建首版")
    return registry
