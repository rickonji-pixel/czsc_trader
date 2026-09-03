from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone

from paper_trading_engine.contracts import OrderSpec
from paper_trading_engine.engine import OrderIntent, PaperTradingEngine
from paper_trading_engine.store import PaperStore
from pte_support import FakeAdvice, FakeBroker, broker_snapshot, decision, make_engine


def test_ft_pte02_decision_order_fill_restart_and_idempotence(tmp_path):
    engine, store, broker, advice = make_engine(tmp_path)
    engine.refresh_account()
    engine.refresh_decision_if_changed(force=True)
    assert len(broker.placed) == 1
    assert store.get_intent(broker.placed[0].intent_id)["status"] == "SUBMITTED"

    engine.refresh_orders()
    assert len(broker.placed) == 1
    submitted = broker.value.orders[0]
    broker.value = broker_snapshot(
        orders=(replace(submitted, status="FILLED_PART", cumulative_filled_quantity=400, average_fill_price=1.67),),
        quantity=400,
    )
    engine.refresh_orders()
    engine.refresh_orders()
    increments = [e for e in store.recent_events() if e["event_type"] == "FILL_INCREMENT"]
    assert len(increments) == 1
    assert increments[0]["payload"]["quantity"] == 400

    engine.pause()
    assert engine.status()["paused"] is True
    broker.value = broker_snapshot(
        orders=(replace(submitted, status="FILLED_ALL", cumulative_filled_quantity=1000, average_fill_price=1.68),),
        quantity=1000,
    )
    engine.refresh_orders()
    assert sum(e["payload"]["quantity"] for e in store.recent_events() if e["event_type"] == "FILL_INCREMENT") == 1000
    engine.resume()
    token = engine.issue_cancel_token(submitted.channel_order_id)
    engine.confirm_cancel(submitted.channel_order_id, token)
    assert broker.cancelled == [submitted.channel_order_id]
    store.close()

    # A persisted pre-submit intent is safely recovered after a process restart.
    recovery_store = PaperStore(tmp_path / "recovery.db")
    intent = OrderIntent("PTE-DEC-ONE", "DEC-ONE", "588080.SH", "BUY", 1000, 1.68)
    recovery_store.save_intent(intent.intent_id, intent.decision_id, asdict(intent))
    recovery_broker = FakeBroker()
    recovery = PaperTradingEngine(
        recovery_store, recovery_broker, FakeAdvice(decision(OrderSpec("BUY", 1000, "LIMIT", 1.68, "DAY"))),
        symbol="588080.SH", today=lambda: date(2026, 9, 2),
        now=lambda: datetime(2026, 9, 2, 9, 30, tzinfo=timezone(timedelta(hours=8))),
    )
    recovery.refresh()
    assert len(recovery_broker.placed) == 1
    assert recovery_store.get_intent(intent.intent_id)["channel_order_id"] == "1001"
    recovery_store.close()

    # Multi-slice decisions submit the next slice only after the active one settles.
    first = OrderSpec("BUY", 1_000_000, "LIMIT", 1.0, "DAY")
    second = OrderSpec("BUY", 100, "LIMIT", 1.0, "DAY")
    multi_decision = replace(
        decision(), action="BUY", target_quantity=1_000_100,
        cycle_target_quantity=1_000_100, delta_quantity=1_000_100,
        orders=(first, second),
    )
    multi_store = PaperStore(tmp_path / "multi.db")
    multi_broker = FakeBroker()
    multi = PaperTradingEngine(
        multi_store, multi_broker, FakeAdvice(multi_decision), symbol="588080.SH",
        today=lambda: date(2026, 9, 2),
        now=lambda: datetime(2026, 9, 2, 9, 30, tzinfo=timezone(timedelta(hours=8))),
    )
    multi.refresh()
    assert [item.quantity for item in multi_broker.placed] == [1_000_000]
    active = multi_broker.value.orders[0]
    multi_broker.value = broker_snapshot(orders=(replace(active, status="FILLED_ALL"),))
    multi.refresh_orders()
    assert [item.quantity for item in multi_broker.placed] == [1_000_000, 100]
    multi_store.close()
