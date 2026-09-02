from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from paper_trading_engine.contracts import AdviceDecision, OrderSpec


def decision(order: OrderSpec | None = None) -> AdviceDecision:
    quantity = 0 if order is None else order.quantity
    return AdviceDecision(
        contract_version="advice.v1",
        decision_id="DEC-ONE",
        symbol="588080.SH",
        signal_date=date(2026, 9, 1),
        valid_session=date(2026, 9, 2),
        actual_quantity=0,
        target_quantity=quantity,
        position_size=50_000,
        delta_quantity=quantity,
        action="WAIT" if order is None else "BUY",
        baseline={"version": "baseline_20260901", "sha256": "b" * 64},
        execution_policy={"version": "execution_policy_20260902", "sha256": "e" * 64},
        signal_reference_price=1.704,
        execution_reference_price=1.688,
        data_cutoff=date(2026, 9, 1),
        order=order,
    )


class FakeAdvice:
    def __init__(self, value: AdviceDecision):
        self.value = value
        self.calls: list[int] = []

    def get_decision(self, actual_quantity: int) -> AdviceDecision:
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
    return PaperTradingEngine(store, broker, advice, symbol="588080.SH"), store, broker, advice


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


def test_resume_requires_a_successful_reconciliation(tmp_path: Path) -> None:
    from paper_trading_engine.engine import PaperTradingStateError

    engine, _, _, _ = make_engine(tmp_path)
    engine.pause()
    with pytest.raises(PaperTradingStateError, match="reconciliation"):
        engine.resume()
    engine.refresh()
    engine.resume()
    assert engine.status()["paused"] is False


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
