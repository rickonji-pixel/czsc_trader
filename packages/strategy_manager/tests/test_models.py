import hashlib
import json

import pytest

from strategy_manager import (
    LifecycleEvent,
    PerformanceEvidence,
    Qualification,
    Strategy,
    StrategyVersion,
    ValidationError,
    canonical_sha256,
)


STRATEGY = {
    "schema_version": 1,
    "strategy_id": "S001",
    "name": "综合基线策略",
    "objective": "改善震荡期表现并保留趋势捕获能力",
    "responsibility": "生成目标仓位，执行模块负责订单与成交",
    "scope": ["588080.SH"],
    "created_at": "2026-09-03T10:00:00+08:00",
    "created_by": "tomxiao",
}

RESEARCH_VERSION = {
    "schema_version": 1,
    "strategy_id": "S001",
    "version": "v1",
    "release_id": "S001-v1",
    "parent_version": None,
    "change_summary": "首个冻结版本",
    "source_experiment": "experiments/0901_EX20",
    "source_candidate": 143,
    "selection_data_cutoff": "2026-09-02",
    "forward_start": "2026-09-03",
    "strategy_payload": {"symbol": "588080.SH", "fee_rate": 0.0003},
    "release_hash": None,
}


def test_canonical_sha256_is_utf8_sorted_and_compact():
    payload = {"中文": "策略", "b": 2, "a": [1, True]}
    expected = hashlib.sha256(
        '{"a":[1,true],"b":2,"中文":"策略"}'.encode("utf-8")
    ).hexdigest()

    assert canonical_sha256(payload) == expected


def test_strategy_round_trip_rejects_unknown_fields():
    strategy = Strategy.from_dict(STRATEGY)
    assert strategy.to_dict() == STRATEGY

    with pytest.raises(ValidationError, match="unknown fields"):
        Strategy.from_dict({**STRATEGY, "runtime_status": "RUNNING"})


@pytest.mark.parametrize("strategy_id", ["baseline-143", "S01", "s001", "S1000"])
def test_strategy_rejects_noncanonical_ids(strategy_id):
    with pytest.raises(ValidationError, match="strategy_id"):
        Strategy.from_dict({**STRATEGY, "strategy_id": strategy_id})


def test_frozen_version_hash_covers_release_payload():
    release_payload = {key: value for key, value in RESEARCH_VERSION.items() if key != "release_hash"}
    expected_hash = canonical_sha256(release_payload)
    version = StrategyVersion.from_dict({**RESEARCH_VERSION, "release_hash": expected_hash})

    assert version.release_id == "S001-v1"
    assert version.release_hash == expected_hash
    assert version.release_payload() == release_payload


def test_version_rejects_mismatched_release_identity():
    with pytest.raises(ValidationError, match="release_id"):
        StrategyVersion.from_dict({**RESEARCH_VERSION, "release_id": "S001-v2"})

    with pytest.raises(ValidationError, match="release_hash"):
        StrategyVersion.from_dict({**RESEARCH_VERSION, "release_hash": "0" * 64})


def test_lifecycle_and_evidence_keep_qualification_separate_from_runtime():
    event = LifecycleEvent.from_dict(
        {
            "schema_version": 1,
            "event_id": "EVT-20260903-0001",
            "event_type": "VERSION_FROZEN",
            "strategy_id": "S001",
            "version": "v1",
            "from_state": "RESEARCH",
            "to_state": "PAPER_READY",
            "occurred_at": "2026-09-03T10:00:00+08:00",
            "actor": "tomxiao",
            "reason": "进入前瞻观察",
            "evidence_ids": ["EVD-20260903-0001"],
            "release_hash": "a" * 64,
        }
    )
    assert event.to_state is Qualification.PAPER_READY
    assert "runtime_status" not in event.to_dict()

    evidence = PerformanceEvidence.from_dict(
        {
            "schema_version": 1,
            "evidence_id": "EVD-20260903-0001",
            "strategy_id": "S001",
            "version": "v1",
            "release_hash": "a" * 64,
            "phase": "RESEARCH_BACKTEST",
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
            "source_path": "experiments/0901_EX20/results.json",
            "source_hash": "b" * 64,
            "recorded_at": "2026-09-03T10:00:00+08:00",
            "recorded_by": "tomxiao",
        }
    )
    assert json.loads(json.dumps(evidence.to_dict(), ensure_ascii=False))["phase"] == (
        "RESEARCH_BACKTEST"
    )
