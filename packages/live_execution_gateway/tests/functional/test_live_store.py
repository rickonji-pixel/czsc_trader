from decimal import Decimal
from pathlib import Path

import pytest

from live_execution_gateway.live_store import LiveTradeStore


def _register(store: LiveTradeStore) -> None:
    store.register_deployment(
        account_id="live-1", strategy_id="S103", strategy_version="v1",
        release_hash="abc", symbol="MU.US",
        max_order_notional=Decimal("10000"),
        max_gross_notional=Decimal("20000"), cash_reserve=Decimal("100"),
    )


def _decision(source="SRT-1") -> dict:
    return {
        "decision_id": source, "source_decision_id": source, "symbol": "MU.US",
        "signal_date": "2026-09-22", "valid_session": "2026-09-23",
        "orders": [{
            "side": "BUY", "quantity": 2, "order_type": "MARKET",
            "limit_price": 100.0, "time_in_force": "DAY",
        }],
        "plan_legs": [],
    }


def test_deployment_is_disarmed_and_shadow_intents_cannot_be_promoted(tmp_path: Path) -> None:
    store = LiveTradeStore(tmp_path / "live.db")
    try:
        _register(store)
        result = store.save_decision_and_intents(
            account_id="live-1", decision=_decision(),
            qualification="PAPER_READY", executable=False,
        )
        challenge = store.issue_arm_challenge("live-1")
        store.arm(
            "live-1", challenge_id=challenge["challenge_id"],
            confirmation_phrase=challenge["confirmation_phrase"], hours=1,
            actor="owner", reason="test",
        )
        repeated = store.save_decision_and_intents(
            account_id="live-1", decision=_decision(),
            qualification="LIVE_READY", executable=True,
        )
        intents = store.intents("live-1")
    finally:
        store.close()

    assert result["disposition"] == "SHADOW_ONLY"
    assert repeated["disposition"] == "SHADOW_ONLY"
    assert intents[0]["status"] == "SHADOW_ONLY"


def test_arm_requires_one_time_phrase_and_expires_automatically(tmp_path: Path) -> None:
    store = LiveTradeStore(tmp_path / "live.db")
    try:
        _register(store)
        challenge = store.issue_arm_challenge("live-1")
        with pytest.raises(ValueError, match="phrase differs"):
            store.arm(
                "live-1", challenge_id=challenge["challenge_id"],
                confirmation_phrase="wrong", hours=1, actor="owner", reason="test",
            )
        armed = store.arm(
            "live-1", challenge_id=challenge["challenge_id"],
            confirmation_phrase=challenge["confirmation_phrase"], hours=1,
            actor="owner", reason="test",
        )
        with pytest.raises(ValueError, match="already used"):
            store.arm(
                "live-1", challenge_id=challenge["challenge_id"],
                confirmation_phrase=challenge["confirmation_phrase"], hours=1,
                actor="owner", reason="test",
            )
    finally:
        store.close()

    assert armed["status"] == "ARMED"
    assert armed["arm_generation"] == 1


def test_risk_limits_change_only_while_disarmed(tmp_path: Path) -> None:
    store = LiveTradeStore(tmp_path / "live.db")
    try:
        _register(store)
        updated = store.update_risk_limits(
            "live-1", max_order_notional=Decimal("5000"),
            max_gross_notional=Decimal("10000"), cash_reserve=Decimal("250"),
        )
        challenge = store.issue_arm_challenge("live-1")
        store.arm(
            "live-1", challenge_id=challenge["challenge_id"],
            confirmation_phrase=challenge["confirmation_phrase"], hours=1,
            actor="owner", reason="test",
        )
        with pytest.raises(PermissionError, match="DISARMED"):
            store.update_risk_limits(
                "live-1", max_order_notional=Decimal("4000"),
                max_gross_notional=Decimal("8000"), cash_reserve=Decimal("100"),
            )
    finally:
        store.close()

    assert updated["max_order_notional"] == "5000"


def test_prepare_is_idempotent_after_operator_changes_limits(tmp_path: Path) -> None:
    store = LiveTradeStore(tmp_path / "live.db")
    try:
        _register(store)
        store.update_risk_limits(
            "live-1", max_order_notional=Decimal("5000"),
            max_gross_notional=Decimal("10000"), cash_reserve=Decimal("250"),
        )
        _register(store)
        deployment = store.deployment("live-1")
    finally:
        store.close()

    assert deployment["max_order_notional"] == "5000"
    assert deployment["max_gross_notional"] == "10000"


def test_disarm_invalidates_ready_intent_and_blocked_state_cannot_rearm(tmp_path: Path) -> None:
    store = LiveTradeStore(tmp_path / "live.db")
    try:
        _register(store)
        challenge = store.issue_arm_challenge("live-1")
        store.arm(
            "live-1", challenge_id=challenge["challenge_id"],
            confirmation_phrase=challenge["confirmation_phrase"], hours=1,
            actor="owner", reason="test",
        )
        store.save_decision_and_intents(
            account_id="live-1", decision=_decision(),
            qualification="LIVE_READY", executable=True,
        )
        store.disarm("live-1", reason="operator stop", blocked=True)
        intents = store.intents("live-1")
        with pytest.raises(PermissionError, match="DISARMED"):
            store.issue_arm_challenge("live-1")
    finally:
        store.close()

    assert intents[0]["status"] == "INVALIDATED_BY_DISARM"


def test_extended_session_is_persisted_and_market_order_is_rejected(tmp_path: Path) -> None:
    store = LiveTradeStore(tmp_path / "live.db")
    try:
        _register(store)
        decision = _decision()
        decision["orders"][0].update(order_type="LIMIT", outside_rth="OVERNIGHT")
        store.save_decision_and_intents(
            account_id="live-1", decision=decision,
            qualification="PAPER_READY", executable=False,
        )
        assert store.intents("live-1")[0]["outside_rth"] == "OVERNIGHT"

        invalid = _decision("SRT-2")
        invalid["orders"][0]["outside_rth"] = "ANY_TIME"
        with pytest.raises(ValueError, match="must be LIMIT"):
            store.save_decision_and_intents(
                account_id="live-1", decision=invalid,
                qualification="PAPER_READY", executable=False,
            )
    finally:
        store.close()
