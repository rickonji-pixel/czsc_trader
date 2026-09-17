from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

import pytest

from strategy_runtime import (
    AccountSnapshot,
    DeploymentSpec,
    ExecutionPolicy,
    ExecutionRequest,
    RuntimeCompatibilityError,
    StrategyDecision,
    StrategyLoader,
    StrategyRelease,
    build_execution_plan,
)


NOW = datetime.fromisoformat("2026-09-17T10:00:00+08:00")


def test_every_active_frozen_release_has_a_matching_source_binding() -> None:
    root = Path(__file__).resolve().parents[4]
    for strategy_id, version in (
        ("S001", "v1"),
        ("S001", "v2"),
        ("S002", "v1"),
        ("S003", "v1"),
        ("S007", "v1"),
    ):
        payload = json.loads(
            (root / "strategies" / strategy_id / "versions" / f"{version}.json").read_text(
                encoding="utf-8"
            )
        )
        strategy = StrategyLoader().load(StrategyRelease.from_mapping(payload))
        assert strategy.definition.implementation.source_sha256


def test_loader_rejects_source_that_differs_from_frozen_binding(monkeypatch) -> None:
    root = Path(__file__).resolve().parents[4]
    payload = json.loads(
        (root / "strategies/S007/versions/v1.json").read_text(encoding="utf-8")
    )
    monkeypatch.setattr("strategy_runtime.loader.implementation_sha256", lambda _files: "0" * 64)

    with pytest.raises(RuntimeCompatibilityError, match="differs from runtime binding"):
        StrategyLoader().load(StrategyRelease.from_mapping(payload))


def test_target_execution_plan_honors_frozen_limit_exit() -> None:
    release_hash = "a" * 64
    deployment = DeploymentSpec(
        "pte:test",
        "S007-v1",
        release_hash,
        "588080.SH",
        "test",
        "futu_simulate_cn",
        {"cycle_target_quantity": 1000},
    )
    account = AccountSnapshot("test", 100.0, 1600.0, 1000, 0, NOW)
    decision = StrategyDecision(
        "DEC-TEST",
        deployment.deployment_id,
        deployment.release_id,
        release_hash,
        "b" * 64,
        NOW,
        datetime.fromisoformat("2026-09-18T09:30:00+08:00"),
        0.0,
        0,
        0,
        {"market": "c" * 64},
        {},
        {},
    )
    policy = ExecutionPolicy(
        "FROZEN_RULE",
        {
            "capital": {
                "allocation_fraction": 1.0,
                "fee_rate": 0.001,
                "mode": "full_available_cash",
                "target_scope": "entry_cycle",
            },
            "entry": {"limit_parameter": 0.2, "order_type": "LIMIT"},
            "exit": {"limit_ratio": 0.2, "order_type": "LIMIT"},
            "instrument": {
                "lot_size": 100,
                "maximum_order_quantity": 1000000,
                "price_limit_ratio": 0.2,
                "price_tick": 0.001,
            },
        },
    )

    plan = build_execution_plan(
        ExecutionRequest(deployment, account, decision, policy),
        signal_reference_price=1.6,
        execution_reference_price=1.5,
    )

    assert plan["action"] == "SELL"
    assert plan["orders"][0]["order_type"] == "LIMIT"
    assert plan["orders"][0]["limit_price"] < 1.5


def test_target_execution_plan_preserves_audited_marketable_exit_semantics() -> None:
    release_hash = "a" * 64
    deployment = DeploymentSpec(
        "pte:test",
        "S007-v1",
        release_hash,
        "588080.SH",
        "test",
        "futu_simulate_cn",
        {"cycle_target_quantity": 1000},
    )
    account = AccountSnapshot("test", 100.0, 1600.0, 1000, 0, NOW)
    decision = StrategyDecision(
        "DEC-TEST-MARKET-EXIT",
        deployment.deployment_id,
        deployment.release_id,
        release_hash,
        "b" * 64,
        NOW,
        datetime.fromisoformat("2026-09-18T09:30:00+08:00"),
        0.0,
        0,
        0,
        {"market": "c" * 64},
        {},
        {},
    )
    policy = ExecutionPolicy(
        "FROZEN_RULE",
        {
            "capital": {
                "allocation_fraction": 1.0,
                "fee_rate": 0.001,
                "mode": "full_available_cash",
                "target_scope": "entry_cycle",
            },
            "entry": {"limit_parameter": 0.2, "order_type": "LIMIT"},
            "exit": {"limit_ratio": 0.2, "order_type": "LIMIT"},
            "instrument": {
                "lot_size": 100,
                "maximum_order_quantity": 1000000,
                "price_limit_ratio": 0.2,
                "price_tick": 0.001,
            },
            "virtual_fill": {"sell": "marketable_limit_at_open"},
        },
    )

    plan = build_execution_plan(
        ExecutionRequest(deployment, account, decision, policy),
        signal_reference_price=1.5,
        execution_reference_price=1.5,
    )

    assert plan["action"] == "SELL"
    assert plan["orders"][0]["order_type"] == "MARKET"
    assert plan["orders"][0]["limit_price"] == 1.5
