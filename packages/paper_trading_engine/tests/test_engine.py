from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

from paper_trading_engine.contracts import AdviceDecision, OrderSpec


def decision(order: OrderSpec | None = None) -> AdviceDecision:
    quantity = 0 if order is None else order.quantity
    return AdviceDecision(
        contract_version="advice.v4",
        decision_id="DEC-ONE",
        symbol="588080.SH",
        signal_date=date(2026, 9, 1),
        valid_session=date(2026, 9, 2),
        actual_quantity=0,
        target_quantity=quantity,
        cycle_target_quantity=quantity,
        delta_quantity=quantity,
        action="WAIT" if order is None else "BUY",
        strategy={
            "strategy_id": "S001",
            "name": "综合基线策略",
            "version": "v1",
            "release_id": "S001-v1",
            "release_hash": "b" * 64,
            "qualification": "PAPER_READY",
        },
        signal_reference_price=1.704,
        execution_reference_price=1.688,
        data_cutoff=date(2026, 9, 1),
        order=order,
        orders=() if order is None else (order,),
        available_cash=1_000_000.0,
        fee_rate=0.0005,
        estimated_order_cost=0.0,
        unallocated_cash=1_000_000.0,
    )


class FakeAdvice:
    def __init__(self, value: AdviceDecision):
        self.value = value
        self.calls: list[int] = []
        self.identity = "data-v1"

    def data_identity(self) -> str:
        return self.identity

    def get_decision(
        self, actual_quantity: int, available_cash: float,
        cycle_target_quantity: int | None = None, strategy_id: str | None = None,
        strategy_version: str | None = None, baseline: str | None = None,
    ) -> AdviceDecision:
        self.calls.append(actual_quantity)
        return replace(self.value, actual_quantity=actual_quantity)


def broker_values():
    from paper_trading_engine.engine import BrokerAccount, BrokerSnapshot

    return BrokerSnapshot(
        account=BrokerAccount(
            environment="SIMULATE",
            market="CN",
            cash=1_000_000.0,
            total_assets=1_000_000.0,
            frozen_cash=0.0,
        ),
        positions=(),
        orders=(),
        quote_health="DEGRADED_QUOTE",
    )


class FakeBroker:
    def __init__(self, snapshot=None):
        self.current = snapshot or broker_values()
        self.placed = []
        self.cancelled = []
        self.store = None

    def snapshot(self):
        return self.current

    def account_snapshot(self):
        return replace(self.current, orders=())

    def order_snapshot(self):
        return self.current.orders

    def place_order(self, intent):
        from paper_trading_engine.engine import BrokerOrder

        assert self.store.get_intent(intent.intent_id) is not None
        self.placed.append(intent)
        order = BrokerOrder(
            channel_order_id="1001",
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            limit_price=intent.limit_price,
            status="SUBMITTED",
            cumulative_filled_quantity=0,
            average_fill_price=0.0,
            remark=intent.intent_id,
        )
        self.current = replace(self.current, orders=(order,))
        return order

    def cancel_order(self, channel_order_id: str) -> None:
        self.cancelled.append(channel_order_id)


def make_engine(tmp_path: Path, *, broker=None, advice=None):
    from paper_trading_engine.engine import PaperTradingEngine
    from paper_trading_engine.store import PaperStore

    store = PaperStore(tmp_path / "runtime.db")
    broker = broker or FakeBroker()
    broker.store = store
    advice = advice or FakeAdvice(
        decision(OrderSpec("BUY", 50_000, "LIMIT", 1.688, "DAY"))
    )
    return PaperTradingEngine(
        store,
        broker,
        advice,
        symbol="588080.SH",
        today=lambda: date(2026, 9, 2),
        now=lambda: datetime.fromisoformat("2026-09-02T09:30:00+08:00"),
    ), store, broker, advice


def test_engine_rejects_non_simulate_or_wrong_market_before_advice(tmp_path: Path) -> None:
    from paper_trading_engine.engine import PaperTradingSafetyError

    snapshot = broker_values()
    broker = FakeBroker(replace(snapshot, account=replace(snapshot.account, environment="REAL")))
    engine, _, _, advice = make_engine(tmp_path, broker=broker)

    with pytest.raises(PaperTradingSafetyError, match="SIMULATE"):
        engine.refresh()
    assert advice.calls == []


def test_engine_persists_intent_before_submit_and_is_idempotent(tmp_path: Path) -> None:
    engine, store, broker, _ = make_engine(tmp_path)

    first = engine.refresh()
    second = engine.refresh()

    assert first["last_decision"]["decision_id"] == "DEC-ONE"
    assert len(broker.placed) == 1
    assert store.get_intent("PTE-DEC-ONE") is not None
    assert second["orders"][0]["channel_order_id"] == "1001"


def test_split_orders_submit_one_slice_at_a_time(tmp_path: Path) -> None:
    from paper_trading_engine.engine import BrokerOrder

    class MultiBroker(FakeBroker):
        def place_order(self, intent):
            number = str(1001 + len(self.placed))
            self.placed.append(intent)
            order = BrokerOrder(number, intent.symbol, intent.side, intent.quantity,
                                intent.limit_price, "SUBMITTED", 0, 0.0, intent.intent_id)
            self.current = replace(self.current, orders=self.current.orders + (order,))
            return order

    one = OrderSpec("BUY", 1_000_000, "LIMIT", 1.0, "DAY")
    two = OrderSpec("BUY", 100, "LIMIT", 1.0, "DAY")
    value = replace(decision(None), action="BUY", target_quantity=1_000_100,
                    cycle_target_quantity=1_000_100, delta_quantity=1_000_100,
                    order=None, orders=(one, two))
    broker = MultiBroker()
    engine, store, broker, _ = make_engine(tmp_path, broker=broker, advice=FakeAdvice(value))

    engine.refresh()
    assert [item.quantity for item in broker.placed] == [1_000_000]
    first = broker.current.orders[0]
    broker.current = replace(broker.current, orders=(replace(first, status="FILLED_ALL"),))
    engine.refresh_orders()
    assert [item.quantity for item in broker.placed] == [1_000_000, 100]
    assert store.find_intent_by_decision("DEC-ONE:1") is not None


def test_partial_fill_does_not_submit_again_while_original_order_is_active(tmp_path: Path) -> None:
    from paper_trading_engine.engine import BrokerPosition

    class QuantityAwareAdvice:
        def get_decision(
            self, actual_quantity: int, available_cash: float,
            cycle_target_quantity: int | None = None, strategy_id: str | None = None,
            strategy_version: str | None = None, baseline: str | None = None,
        ) -> AdviceDecision:
            remaining = 50_000 - actual_quantity
            return replace(
                decision(OrderSpec("BUY", remaining, "LIMIT", 1.688, "DAY")),
                decision_id=f"DEC-{actual_quantity}",
                actual_quantity=actual_quantity,
                target_quantity=50_000,
                delta_quantity=remaining,
            )

    engine, _, broker, _ = make_engine(tmp_path, advice=QuantityAwareAdvice())
    engine.refresh()
    submitted = broker.current.orders[0]
    broker.current = replace(
        broker.current,
        positions=(BrokerPosition("588080.SH", 10_000),),
        orders=(replace(submitted, status="FILLED_PART", cumulative_filled_quantity=10_000),),
    )

    status = engine.refresh()

    assert len(broker.placed) == 1
    assert "ACTIVE_ORDER_BLOCKS_SUBMISSION" in status["alerts"]


def test_reconciliation_records_only_monotonic_fill_increments(tmp_path: Path) -> None:
    engine, store, broker, _ = make_engine(tmp_path)
    engine.refresh()
    submitted = broker.current.orders[0]
    broker.current = replace(
        broker.current,
        orders=(replace(submitted, cumulative_filled_quantity=10_000, average_fill_price=1.687),),
    )
    engine.refresh()
    engine.refresh()
    broker.current = replace(
        broker.current,
        orders=(replace(submitted, cumulative_filled_quantity=5_000, average_fill_price=1.687),),
    )

    with pytest.raises(ValueError, match="decrease"):
        engine.refresh()
    fills = [event for event in store.recent_events() if event["event_type"] == "FILL_INCREMENT"]
    assert len(fills) == 1
    assert fills[0]["payload"]["quantity"] == 10_000


def test_pause_blocks_new_orders_but_keeps_reconciliation(tmp_path: Path) -> None:
    engine, _, broker, advice = make_engine(tmp_path)
    engine.pause()

    status = engine.refresh()

    assert broker.placed == []
    assert advice.calls == [0]
    assert status["paused"] is True


def test_engine_submits_only_on_decision_valid_session(tmp_path: Path) -> None:
    engine, _, broker, _ = make_engine(tmp_path)
    engine.today = lambda: date(2026, 9, 1)

    status = engine.refresh()

    assert broker.placed == []
    assert "DECISION_NOT_VALID_TODAY" in status["alerts"]


def test_engine_waits_until_submission_window_for_cached_decision(tmp_path: Path) -> None:
    engine, _, broker, _ = make_engine(tmp_path)
    engine.now = lambda: datetime.fromisoformat("2026-09-02T00:00:00+08:00")

    status = engine.refresh()
    assert broker.placed == []
    assert "OUTSIDE_SUBMISSION_WINDOW" in status["alerts"]

    engine.now = lambda: datetime.fromisoformat("2026-09-02T09:30:00+08:00")
    engine.refresh_orders()
    assert len(broker.placed) == 1


def test_decision_recomputes_only_when_data_identity_or_quantity_changes(tmp_path: Path) -> None:
    engine, _, _, advice = make_engine(tmp_path, advice=FakeAdvice(decision()))
    engine.refresh_account()

    engine.refresh_decision_if_changed()
    engine.refresh_decision_if_changed()
    advice.identity = "data-v2"
    engine.refresh_decision_if_changed()

    assert advice.calls == [0, 0]


def test_order_poll_rejects_invalid_lot_before_reconciliation(tmp_path: Path) -> None:
    from paper_trading_engine.engine import BrokerOrder, PaperTradingSafetyError

    broker = FakeBroker()
    broker.current = replace(
        broker.current,
        orders=(
            BrokerOrder(
                channel_order_id="bad-lot",
                symbol="588080.SH",
                side="BUY",
                quantity=50,
                limit_price=1.688,
                status="SUBMITTED",
                cumulative_filled_quantity=0,
                average_fill_price=0.0,
                remark="external",
            ),
        ),
    )
    engine, store, _, _ = make_engine(tmp_path, broker=broker, advice=FakeAdvice(decision()))
    engine.refresh_account()

    with pytest.raises(PaperTradingSafetyError, match="100-share lots"):
        engine.refresh_orders()
    assert store.orders() == []


def test_resume_requires_a_successful_reconciliation(tmp_path: Path) -> None:
    from paper_trading_engine.engine import PaperTradingStateError

    engine, _, _, _ = make_engine(tmp_path)
    engine.pause()
    with pytest.raises(PaperTradingStateError, match="reconciliation"):
        engine.resume()
    engine.refresh()
    engine.resume()
    assert engine.status()["paused"] is False


def test_status_surfaces_active_data_publication_alert(tmp_path: Path) -> None:
    engine, store, _, _ = make_engine(tmp_path, advice=FakeAdvice(decision()))
    engine.refresh()
    store.set_setting("data_publication_error", "vendor unavailable")

    assert "DATA_PUBLICATION_FAILED" in engine.status()["alerts"]


def test_cancel_token_is_bound_and_single_use(tmp_path: Path) -> None:
    from paper_trading_engine.engine import PaperTradingStateError

    engine, _, broker, _ = make_engine(tmp_path)
    engine.refresh()
    token = engine.issue_cancel_token("1001")

    with pytest.raises(PaperTradingStateError, match="bound"):
        engine.confirm_cancel("other", token)
    engine.confirm_cancel("1001", token)
    with pytest.raises(PaperTradingStateError, match="invalid"):
        engine.confirm_cancel("1001", token)
    assert broker.cancelled == ["1001"]


def test_store_reopen_preserves_pause_and_intent(tmp_path: Path) -> None:
    from paper_trading_engine.store import PaperStore

    engine, store, _, _ = make_engine(tmp_path)
    engine.refresh()
    engine.pause()
    store.close()

    reopened = PaperStore(tmp_path / "runtime.db")
    assert reopened.is_paused() is True
    assert reopened.get_intent("PTE-DEC-ONE") is not None


def test_restart_retries_persisted_pending_intent_when_broker_has_no_order(tmp_path: Path) -> None:
    from dataclasses import asdict
    from paper_trading_engine.engine import OrderIntent

    engine, store, broker, _ = make_engine(tmp_path)
    intent = OrderIntent("PTE-DEC-ONE", "DEC-ONE", "588080.SH", "BUY", 50_000, 1.688)
    store.save_intent(intent.intent_id, intent.decision_id, asdict(intent))

    engine.refresh()

    assert len(broker.placed) == 1
    assert store.get_intent(intent.intent_id)["channel_order_id"] == "1001"


def test_restart_binds_persisted_intent_to_broker_order_by_remark(tmp_path: Path) -> None:
    from dataclasses import asdict
    from paper_trading_engine.engine import BrokerOrder, OrderIntent

    engine, store, broker, _ = make_engine(tmp_path)
    intent = OrderIntent("PTE-DEC-ONE", "DEC-ONE", "588080.SH", "BUY", 50_000, 1.688)
    store.save_intent(intent.intent_id, intent.decision_id, asdict(intent))
    broker.current = replace(
        broker.current,
        orders=(BrokerOrder("existing", "588080.SH", "BUY", 50_000, 1.688,
                            "SUBMITTED", 0, 0.0, intent.intent_id),),
    )

    engine.refresh()

    assert broker.placed == []
    assert store.get_intent(intent.intent_id)["channel_order_id"] == "existing"
