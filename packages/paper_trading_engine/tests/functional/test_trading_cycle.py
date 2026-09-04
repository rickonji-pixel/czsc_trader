from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from subprocess import CompletedProcess

import pytest

from paper_trading_engine.advice_client import AdviceClientError, CliAdviceClient
from paper_trading_engine.audit import AuditRecorder
from paper_trading_engine.contracts import OrderSpec
from paper_trading_engine.engine import OrderIntent, PaperTradingEngine
from paper_trading_engine.store import PaperStore
from pte_support import FakeAdvice, FakeBroker, broker_snapshot, decision, make_engine


def test_ft_pte02_decision_order_fill_restart_and_idempotence(tmp_path, monkeypatch):
    engine, store, broker, advice = make_engine(tmp_path)
    engine.refresh_account()
    engine.refresh_decision_if_changed(force=True)
    assert len(broker.placed) == 1
    assert store.get_intent(broker.placed[0].intent_id)["status"] == "SUBMITTED"
    initial_events = store.query_audit_events(correlation_id="DEC-ONE", limit=20)
    assert {event["event_type"] for event in initial_events} >= {
        "DECISION_GENERATED", "SIGNAL_TRIGGERED", "ORDER_INTENT_CREATED", "ORDER_SUBMITTED",
    }
    assert all(event["strategy_id"] == "S001" for event in initial_events)
    before_repeat = len(store.query_audit_events(correlation_id="DEC-ONE", limit=20))

    engine.refresh_orders()
    assert len(broker.placed) == 1
    after_block = len(store.query_audit_events(correlation_id="DEC-ONE", limit=20))
    assert after_block == before_repeat + 1
    engine.refresh_orders()
    assert len(store.query_audit_events(correlation_id="DEC-ONE", limit=20)) == after_block
    submitted = broker.value.orders[0]
    broker.value = broker_snapshot(
        orders=(replace(submitted, status="FILLED_PART", cumulative_filled_quantity=400, average_fill_price=1.67),),
        quantity=400,
    )
    engine.refresh_orders()
    engine.refresh_orders()
    increments = [
        event for event in store.recent_events()
        if event["event_type"] == "ORDER_PARTIALLY_FILLED"
    ]
    assert len(increments) == 1
    assert increments[0]["payload"]["quantity"] == 400

    engine.pause()
    assert engine.status()["paused"] is True
    broker.value = broker_snapshot(
        orders=(replace(submitted, status="FILLED_ALL", cumulative_filled_quantity=1000, average_fill_price=1.68),),
        quantity=1000,
    )
    engine.refresh_orders()
    assert sum(
        event["details"]["quantity"] for event in store.recent_events()
        if event["event_type"] in {"ORDER_PARTIALLY_FILLED", "ORDER_FILLED"}
    ) == 1000
    assert store.query_audit_events(event_type="ORDER_FILLED")[0]["order_id"] == "1001"
    engine.resume()
    token = engine.issue_cancel_token(submitted.channel_order_id)
    engine.confirm_cancel(submitted.channel_order_id, token)
    assert broker.cancelled == [submitted.channel_order_id]
    assert store.query_audit_events(event_type="CANCEL_REQUESTED")[0]["order_id"] == "1001"
    assert store.query_audit_events(event_type="CANCEL_SUCCEEDED")[0]["order_id"] == "1001"
    store.close()

    class RejectingBroker(FakeBroker):
        def place_order(self, intent):
            raise RuntimeError("broker rejected")

    rejected_store = PaperStore(tmp_path / "rejected.db")
    rejected_broker = RejectingBroker()
    rejected = PaperTradingEngine(
        rejected_store, rejected_broker,
        FakeAdvice(decision(OrderSpec("BUY", 1000, "LIMIT", 1.68, "DAY"))),
        symbol="588080.SH", today=lambda: date(2026, 9, 2),
        now=lambda: datetime(2026, 9, 2, 9, 30, tzinfo=timezone(timedelta(hours=8))),
    )
    rejected.refresh_account()
    try:
        rejected.refresh_decision_if_changed(force=True)
    except RuntimeError as exc:
        assert str(exc) == "broker rejected"
    else:
        raise AssertionError("broker rejection must propagate")
    failure = rejected_store.query_audit_events(event_type="ORDER_SUBMISSION_FAILED")[0]
    assert failure["outcome"] == "FAILURE"
    assert failure["details"]["side"] == "BUY"
    rejected_store.close()

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

    advice_store = PaperStore(tmp_path / "advice-failure.db")
    client = CliAdviceClient(
        executable="czsc-trader", repo_root=tmp_path, data_dir=tmp_path,
        symbol="588080.SH", asset="etf", audit=AuditRecorder(advice_store),
        runner=lambda args, **kwargs: CompletedProcess(args, 5, "", "provider unavailable"),
    )
    with pytest.raises(AdviceClientError):
        client.get_decision(0, 100_000, strategy_id="S001", strategy_version="v1")
    assert advice_store.query_audit_events(event_type="EXTERNAL_CALL_FAILED")
    failure = advice_store.query_audit_events(event_type="DECISION_GENERATION_FAILED")[0]
    assert failure["strategy_id"] == "S001"
    assert failure["strategy_version"] == "v1"
    advice_store.close()

    atomic_store = PaperStore(tmp_path / "atomic.db")
    atomic_broker = FakeBroker()
    atomic = PaperTradingEngine(
        atomic_store, atomic_broker,
        FakeAdvice(decision(OrderSpec("BUY", 1000, "LIMIT", 1.68, "DAY"))),
        symbol="588080.SH", today=lambda: date(2026, 9, 2),
        now=lambda: datetime(2026, 9, 2, 9, 30, tzinfo=timezone(timedelta(hours=8))),
    )
    original_insert = atomic_store._insert_audit_event

    def reject_intent_event(event):
        if event.event_type == "ORDER_INTENT_CREATED":
            raise RuntimeError("audit unavailable")
        return original_insert(event)

    monkeypatch.setattr(atomic_store, "_insert_audit_event", reject_intent_event)
    atomic.refresh_account()
    with pytest.raises(RuntimeError, match="audit unavailable"):
        atomic.refresh_decision_if_changed(force=True)
    assert atomic_store.get_intent("PTE-DEC-ONE") is None
    assert atomic_broker.placed == []
    atomic_store.close()
