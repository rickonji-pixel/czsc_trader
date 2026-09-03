from pathlib import Path

import pytest

from paper_trading_engine.store import PaperStore


RELEASE_V1 = {
    "strategy_id": "S001",
    "version": "v1",
    "release_id": "S001-v1",
    "release_hash": "a" * 64,
    "qualification": "PAPER_READY",
}
RELEASE_V2 = {
    "strategy_id": "S001",
    "version": "v2",
    "release_id": "S001-v2",
    "release_hash": "b" * 64,
    "qualification": "PAPER_READY",
}


@pytest.fixture
def store(tmp_path: Path):
    value = PaperStore(tmp_path / "runtime.db")
    yield value
    value.close()


def test_binding_round_trip_and_same_release_is_idempotent(store):
    from paper_trading_engine.channel_binding import bind_channel_strategy, load_channel_binding

    first = bind_channel_strategy(store, RELEASE_V2, "tomxiao", "人工确认", [])
    second = bind_channel_strategy(store, RELEASE_V2, "tomxiao", "重复执行", [])

    assert load_channel_binding(store) == first
    assert second == first
    events = [event for event in store.recent_events() if event["event_type"] == "CHANNEL_STRATEGY_BOUND"]
    assert len(events) == 1
    assert events[0]["payload"]["channel"] == "futu"


def test_binding_rejects_switch_with_active_channel_order(store):
    from paper_trading_engine.channel_binding import ChannelBindingError, bind_channel_strategy

    bind_channel_strategy(store, RELEASE_V1, "tomxiao", "初始绑定", [])
    with pytest.raises(ChannelBindingError, match="活动渠道订单"):
        bind_channel_strategy(
            store,
            RELEASE_V2,
            "tomxiao",
            "切换",
            [{"channel_order_id": "1", "status": "SUBMITTED"}],
        )


def test_binding_requires_a_paper_qualified_complete_release(store):
    from paper_trading_engine.channel_binding import ChannelBindingError, bind_channel_strategy

    with pytest.raises(ChannelBindingError, match="资格"):
        bind_channel_strategy(store, {**RELEASE_V2, "qualification": "RESEARCH"}, "tomxiao", "x", [])
    with pytest.raises(ChannelBindingError, match="release_hash"):
        bind_channel_strategy(store, {**RELEASE_V2, "release_hash": "bad"}, "tomxiao", "x", [])


def test_migration_uses_latest_channel_decision_identity(store):
    from paper_trading_engine.channel_binding import migrate_channel_binding

    store.save_snapshot({"last_decision": {"strategy": RELEASE_V2}})
    binding = migrate_channel_binding(store)

    assert binding is not None
    assert binding.release_id == "S001-v2"
    assert binding.bound_by == "migration"
