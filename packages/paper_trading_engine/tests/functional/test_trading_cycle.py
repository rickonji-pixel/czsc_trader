from dataclasses import asdict, replace
from datetime import date, datetime, time, timezone
import re
import pytest

from paper_trading_engine.account_engine import (
    AccountDecisionBlockedError,
    AccountEngine,
    AccountRefreshBatchError,
    ActiveOrderPendingError,
)
from paper_trading_engine.audit import AuditRecorder
from paper_trading_engine.coordinator import PteCoordinator
from paper_trading_engine.contracts import AdviceDecision, OrderSpec, PlanLegSpec
from paper_trading_engine.futu_execution import FutuExecution
from paper_trading_engine.scheduler import RuntimeScheduler
from paper_trading_engine.store import PaperStore
from pte_support import FakeAdvice, FakeBroker, broker_snapshot, decision


def test_blocked_or_draining_account_cannot_complete_a_decision_generation(tmp_path):
    store = PaperStore(tmp_path / "decision-gate.db")
    store.create_virtual_account(
        "s001-v1", "S001-v1模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S001", strategy_name_snapshot="综合基线策略",
        strategy_version="v1", release_hash="b" * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-01",
    )
    store.set_setting("last_data_publish_date", "2026-09-01")
    store.set_virtual_health("s001-v1", "BLOCKED", "等待人工处理")
    advice = FakeAdvice(decision(OrderSpec("BUY", 1000, "LIMIT", 1.68, "DAY")))
    accounts = AccountEngine(store, advice)

    class Publisher:
        def publish(self, end_date):
            raise AssertionError("publication is not due in this test")

    scheduler = RuntimeScheduler(accounts, Publisher(), store)
    scheduler.tick_daily(datetime(2026, 9, 2, 10, 0, 0))
    assert store.get_setting("last_account_decision_date") is None
    assert store.account_decisions("s001-v1") == []
    assert store.account_intents("s001-v1") == []
    assert advice.calls == []
    assert {row["operation"] for row in store.operation_failures()} == {
        "account_decisions"
    }

    store.set_virtual_health("s001-v1", "OK")
    accounts.begin_shutdown()
    with pytest.raises(AccountRefreshBatchError, match="PTE正在停止"):
        accounts.refresh_all()
    assert store.account_decisions("s001-v1") == []
    assert store.account_intents("s001-v1") == []
    store.close()


def test_immediate_split_order_intents_are_persisted_atomically(tmp_path, monkeypatch):
    store = PaperStore(tmp_path / "split-order-atomic.db")
    store.create_virtual_account(
        "s001-v1", "S001-v1模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S001", strategy_name_snapshot="综合基线策略",
        strategy_version="v1", release_hash="b" * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-01",
    )
    audit = AuditRecorder(store)
    events = [
        audit.build(
            "ORDER_INTENT_CREATED", source="test", account_id="s001-v1",
            decision_id="DEC-SPLIT", correlation_id="DEC-SPLIT",
            details={"order_sequence": sequence},
        )
        for sequence in range(2)
    ]
    orders = [
        {
            "sequence": sequence, "side": "BUY", "quantity": 1000,
            "order_type": "LIMIT", "limit_price": 1.68,
        }
        for sequence in range(2)
    ]
    original_insert = store._insert_audit_event
    inserted = 0

    def fail_second_event(event):
        nonlocal inserted
        inserted += 1
        if inserted == 2:
            raise RuntimeError("simulated audit persistence failure")
        original_insert(event)

    monkeypatch.setattr(store, "_insert_audit_event", fail_second_event)
    with pytest.raises(RuntimeError, match="audit persistence"):
        store.create_account_immediate_intents(
            account_id="s001-v1", decision_id="DEC-SPLIT", symbol="588080.SH",
            valid_session="2026-09-02", fee_rate="0.0005",
            orders=orders, audit_events=events,
        )
    account = store.virtual_account("s001-v1")
    assert store.account_intents("s001-v1") == []
    assert float(account["cash"]) == 100_000
    assert float(account["frozen_cash"]) == 0
    assert store.query_audit_events(event_type="ORDER_INTENT_CREATED") == []

    monkeypatch.setattr(store, "_insert_audit_event", original_insert)
    created = store.create_account_immediate_intents(
        account_id="s001-v1", decision_id="DEC-SPLIT", symbol="588080.SH",
        valid_session="2026-09-02", fee_rate="0.0005",
        orders=orders, audit_events=events,
    )
    assert len(created) == 2
    assert float(store.virtual_account("s001-v1")["frozen_cash"]) == 3361.68
    store.close()


def intraday_setup_decision(strategy: dict[str, str]) -> AdviceDecision:
    return AdviceDecision(
        contract_version="advice.v5", decision_id="DEC-CORE-SETUP",
        symbol="510500.SH", signal_date=date(2026, 9, 1),
        valid_session=date(2026, 9, 2), actual_quantity=0,
        target_quantity=1000, cycle_target_quantity=1000, delta_quantity=1000,
        action="BUY", strategy=strategy, signal_reference_price=1.70,
        execution_reference_price=1.70, data_cutoff=date(2026, 9, 1),
        order=None, orders=(), available_cash=100_000, fee_rate=0.0005,
        plan_mode="CORE_SETUP",
        plan_legs=(PlanLegSpec(
            0, "CORE_SETUP", "OPEN", time(9, 30), time(9, 35),
            None, None, OrderSpec("BUY", 1000, "LIMIT", 1.68, "DAY"),
        ),),
    )


def test_recovered_buy_uses_a_new_reservation_generation_and_release(tmp_path):
    store = PaperStore(tmp_path / "generation.db")
    store.create_virtual_account(
        "s003-v1", "S003-v1模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S003", strategy_name_snapshot="成分资金流宽度早盘延续",
        strategy_version="v1", release_hash="c" * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-08",
        symbol="510500.SH",
    )
    [intent] = store.create_account_plan_intents(
        account_id="s003-v1", decision_id="DEC-GENERATION", symbol="510500.SH",
        valid_session="2026-09-14", fee_rate="0.0005",
        legs=[{
            "sequence": 0, "side": "BUY", "quantity": 1000,
            "order_type": "LIMIT", "limit_price": "7.5000",
            "plan_mode": "CORE_SETUP", "role": "CORE_SETUP", "checkpoint": "OPEN",
            "submit_after": "09:30:00", "submit_before": "09:35:00",
            "dependency_sequence": None, "dependency_required_status": None,
        }],
    )
    store.release_account_intent(
        intent["intent_id"], "EXPIRED", attention_reason="计划订单错过提交截止时间",
    )
    store.recover_future_planned_intent(intent["intent_id"])
    recovered = store.account_intent(intent["intent_id"])
    assert recovered["reservation_generation"] == 2
    store.release_account_intent(intent["intent_id"], "REJECTED")
    assert store.account_invariant_violations() == []
    with store._lock:
        releases = store._connection.execute(
            "SELECT 1 FROM account_ledger WHERE account_id=? AND entry_type='INTENT_RELEASE'",
            ("s003-v1",),
        ).fetchall()
    assert len(releases) == 2
    store.release_account_intent(intent["intent_id"], "REJECTED")
    assert store.account_invariant_violations() == []
    store.close()


def test_missing_recovered_release_can_be_repaired_once_with_audit(tmp_path):
    store = PaperStore(tmp_path / "repair.db")
    store.create_virtual_account(
        "s003-v1", "S003-v1模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S003", strategy_name_snapshot="成分资金流宽度早盘延续",
        strategy_version="v1", release_hash="c" * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-08",
        symbol="510500.SH",
    )
    intent = store.create_account_intent(
        account_id="s003-v1", decision_id="DEC-REPAIR", order_sequence=0,
        symbol="510500.SH", side="BUY", quantity=1000,
        limit_price="7.5000", valid_session="2026-09-14",
    )
    reserve = "7503.7500"
    with store._lock, store._connection:
        store._connection.execute(
            "UPDATE virtual_accounts SET cash='100000.0000',frozen_cash='0.0000' "
            "WHERE account_id='s003-v1'"
        )
        store._connection.execute(
            "UPDATE intents SET status='REJECTED',attention_required=1,"
            "attention_reason='broker rejected' WHERE intent_id=?", (intent["intent_id"],)
        )
    assert store.account_invariant_violations()
    event = AuditRecorder(store).build(
        "ACCOUNT_LEDGER_REPAIRED", source="test", actor_type="OPERATOR",
        account_id="s003-v1", correlation_id=intent["intent_id"],
    )
    result = store.repair_released_intent_ledger("s003-v1", intent["intent_id"], event)
    assert result["status"] == "REPAIRED"
    assert result["cash_delta"] == reserve
    assert store.account_invariant_violations() == []
    repeated = store.repair_released_intent_ledger("s003-v1", intent["intent_id"], event)
    assert repeated["status"] == "ALREADY_REPAIRED"
    assert len(store.query_audit_events(event_type="ACCOUNT_LEDGER_REPAIRED")) == 1
    store.close()


def test_ft_pte02_account_decision_futu_order_fill_restart_and_idempotence(tmp_path):
    store = PaperStore(tmp_path / "runtime.db")
    store.create_virtual_account(
        "s001-v1", "S001-v1模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S001", strategy_name_snapshot="综合基线策略",
        strategy_version="v1", release_hash="b" * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-01",
    )
    advice = FakeAdvice(decision(OrderSpec("BUY", 1000, "LIMIT", 1.68, "DAY")))
    accounts = AccountEngine(
        store, advice, now=lambda: datetime(2026, 9, 8, 6, 35, tzinfo=timezone.utc),
    )
    broker = FakeBroker()
    execution = FutuExecution(store, broker, symbol="588080.SH", today=lambda: date(2026, 9, 2))

    accounts.refresh_account("s001-v1")
    accounts.refresh_account("s001-v1")
    assert len(store.account_decisions("s001-v1")) == 1
    assert len(store.pending_account_intents()) == 1
    with pytest.raises(ActiveOrderPendingError, match="禁止人工驱动"):
        accounts.drive_account_decision("s001-v1")
    assert len(advice.calls) == 1
    saved_decision = store.account_decisions("s001-v1")[0]
    assert re.fullmatch(r"DEC-20260908-1435-[0-9A-F]{12}", saved_decision["decision_id"])
    assert saved_decision["payload"]["source_decision_id"] == "DEC-ONE"
    snapshot = store.account_snapshots("s001-v1")[0]
    assert snapshot["session"] == "2026-09-01"
    assert float(snapshot["total_assets"]) == 100_000
    decision_event = store.query_audit_events(
        event_type="DECISION_GENERATED", account_id="s001-v1"
    )[0]
    assert decision_event["channel"] is None

    execution.refresh_account()
    execution.submit_pending()
    execution.submit_pending()
    assert len(broker.placed) == 1
    submitted = broker.value.orders[0]
    broker.value = broker_snapshot(
        orders=(replace(
            submitted, status="FILLED_PART", cumulative_filled_quantity=400,
            average_fill_price=1.67,
        ),), quantity=400,
    )
    execution.refresh_orders()
    execution.refresh_orders()
    assert store.virtual_account("s001-v1")["quantity"] == 400
    assert len(store.account_fills("s001-v1")) == 1

    next_advice = FakeAdvice(replace(
        decision(), decision_id="DEC-TWO", source_decision_id="DEC-TWO",
        signal_date=date(2026, 9, 2), valid_session=date(2026, 9, 3),
        data_cutoff=date(2026, 9, 2), target_quantity=1000,
        cycle_target_quantity=1000,
    ))
    next_accounts = AccountEngine(store, next_advice)
    store.set_setting("last_data_publish_date", "2026-09-02")
    with pytest.raises(AccountRefreshBatchError, match="存在未完成订单"):
        next_accounts.refresh_all()
    assert next_advice.calls == []
    assert store.virtual_account("s001-v1")["health"] == "BLOCKED"
    assert len(store.account_intents("s001-v1")) == 1
    blocked_events = store.query_audit_events(event_type="ORDER_SUBMISSION_BLOCKED")
    assert {row["details"]["reason"] for row in blocked_events} == {
        "manual_decision_with_active_order", "previous_order_active",
    }
    execution.refresh_orders()
    assert store.virtual_account("s001-v1")["health"] == "BLOCKED"

    broker.value = broker_snapshot(
        orders=(replace(
            submitted, status="FILLED_ALL", cumulative_filled_quantity=1000,
            average_fill_price=1.676,
        ),), quantity=1000,
    )
    execution.refresh_orders()
    assert store.virtual_account("s001-v1")["quantity"] == 1000
    assert store.virtual_account("s001-v1")["health"] == "OK"
    next_accounts.refresh_all()
    assert len(next_advice.calls) == 1
    assert len(store.account_decisions("s001-v1")) == 2
    assert len(store.account_intents("s001-v1")) == 1
    assert sum(row["quantity"] for row in store.account_fills("s001-v1")) == 1000
    assert len(store.query_audit_events(event_type="ORDER_FILLED", account_id="s001-v1")) == 1
    store.close()


    reopened = PaperStore(tmp_path / "runtime.db")
    assert reopened.virtual_account("s001-v1")["quantity"] == 1000
    assert len(reopened.account_orders("s001-v1")) == 1
    assert len(reopened.account_fills("s001-v1")) == 2
    reopened.close()

def test_account_snapshot_values_position_with_execution_price(tmp_path):
    store = PaperStore(tmp_path / "valuation.db")
    store.create_virtual_account(
        "s001-v1", "S001-v1模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S001", strategy_name_snapshot="综合基线策略",
        strategy_version="v1", release_hash="b" * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-01",
    )
    with store._lock, store._connection:
        store._connection.execute(
            "UPDATE virtual_accounts SET cash='90000.0000',quantity=1000 "
            "WHERE account_id='s001-v1'"
        )
    advice = replace(
        decision(), actual_quantity=1000, target_quantity=1000,
        cycle_target_quantity=1000, delta_quantity=0,
        signal_reference_price=2.50, execution_reference_price=7.61,
    )
    AccountEngine(store, FakeAdvice(advice)).refresh_account("s001-v1")
    snapshot = store.account_snapshots("s001-v1")[0]
    assert float(snapshot["close"]) == pytest.approx(7.61)
    assert float(snapshot["market_value"]) == pytest.approx(7_610)
    assert float(snapshot["total_assets"]) == pytest.approx(97_610)
    store.close()


def test_ft_pte10_intraday_plan_waits_for_fill_and_recovers_after_restart(tmp_path):
    store = PaperStore(tmp_path / "runtime.db")
    store.create_virtual_account(
        "s003-v1", "S003-v1模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S003", strategy_name_snapshot="成分资金流宽度早盘延续",
        strategy_version="v1", release_hash="c" * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-08",
        symbol="510500.SH",
    )
    broker = FakeBroker()
    strategy = {
        "strategy_id": "S003", "name": "成分资金流宽度早盘延续",
        "version": "v1", "release_id": "S003-v1",
        "release_hash": "c" * 64, "qualification": "PAPER_READY",
    }
    setup = intraday_setup_decision(strategy)
    AccountEngine(store, FakeAdvice(setup)).refresh_account("s003-v1")
    assert store.virtual_account("s003-v1")["cycle_target"] is None
    setup_execution = FutuExecution(
        store, broker, now=lambda: datetime(2026, 9, 2, 1, 30, 5, tzinfo=timezone.utc),
    )
    setup_execution.refresh_account()
    setup_execution.submit_pending(reconcile=False)
    setup_order = broker.value.orders[-1]
    broker.value = broker_snapshot(
        orders=(replace(
            setup_order, status="FILLED_ALL", cumulative_filled_quantity=1000,
            average_fill_price=1.67,
        ),),
        quantity=1000,
        symbol="510500.SH",
    )
    setup_execution.refresh_orders()
    assert store.virtual_account("s003-v1")["cycle_target"] == 1000

    plan = AdviceDecision(
        contract_version="advice.v5",
        decision_id="DEC-PLAN",
        symbol="510500.SH",
        signal_date=date(2026, 9, 2),
        valid_session=date(2026, 9, 3),
        actual_quantity=1000,
        target_quantity=1000,
        cycle_target_quantity=1000,
        delta_quantity=0,
        action="ROTATE",
        strategy=strategy,
        signal_reference_price=1.70,
        execution_reference_price=1.70,
        data_cutoff=date(2026, 9, 2),
        order=None,
        orders=(),
        available_cash=90_000,
        fee_rate=0.0005,
        plan_mode="CORE_EVENT_INTRADAY_ROTATION",
        plan_legs=(
            PlanLegSpec(
                0, "ROTATION_ENTRY", "OPEN", time(9, 30), time(9, 35),
                None, None, OrderSpec("BUY", 1000, "LIMIT", 1.80, "DAY"),
            ),
            PlanLegSpec(
                1, "ROTATION_EXIT", "11:30_CLOSE", time(11, 29), time(11, 30),
                0, "FILLED_ALL", OrderSpec("SELL", 1000, "MARKET", 1.70, "DAY"),
            ),
        ),
    )
    AccountEngine(store, FakeAdvice(plan)).refresh_account("s003-v1")
    intents = store.account_intents("s003-v1")[-2:]
    assert [row["status"] for row in intents] == ["PENDING_SUBMIT", "WAITING_DEPENDENCY"]

    open_execution = FutuExecution(
        store, broker, now=lambda: datetime(2026, 9, 3, 1, 30, 5, tzinfo=timezone.utc),
    )
    open_execution.refresh_account()
    open_execution.submit_pending(reconcile=False)
    assert [row.side for row in broker.placed] == ["BUY", "BUY"]
    assert store.account_intent(intents[1]["intent_id"])["status"] == "WAITING_DEPENDENCY"

    rotation_buy = broker.value.orders[-1]
    broker.value = broker_snapshot(
        quantity=2000, symbol="510500.SH",
        orders=(
            broker.value.orders[0],
            replace(
                rotation_buy, status="FILLED_ALL", cumulative_filled_quantity=1000,
                average_fill_price=1.71,
            ),
        ),
    )
    restarted = FutuExecution(
        store, broker, now=lambda: datetime(2026, 9, 3, 3, 29, 5, tzinfo=timezone.utc),
    )
    restarted.refresh()
    assert [row.side for row in broker.placed] == ["BUY", "BUY", "SELL"]
    assert store.account_intent(intents[1]["intent_id"])["status"] == "SUBMITTED"

    rotation_sell = broker.value.orders[-1]
    broker.value = broker_snapshot(
        quantity=1000, symbol="510500.SH",
        orders=(
            broker.value.orders[0], broker.value.orders[1],
            replace(
                rotation_sell, status="FILLED_ALL", cumulative_filled_quantity=1000,
                average_fill_price=1.72,
            ),
        ),
    )
    restarted.refresh_orders()
    assert store.virtual_account("s003-v1")["quantity"] == 1000
    assert all(
        row["status"] == "FILLED_ALL" for row in store.account_intents("s003-v1")
    )
    assert len(store.query_audit_events(event_type="EXECUTION_PLAN_LEG_READY")) == 1
    store.close()


def test_ft_pte11_intraday_plan_blocks_exit_when_entry_is_not_filled(tmp_path):
    store = PaperStore(tmp_path / "runtime.db")
    store.create_virtual_account(
        "s003-v1", "S003-v1模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S003", strategy_name_snapshot="成分资金流宽度早盘延续",
        strategy_version="v1", release_hash="c" * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-08",
        symbol="510500.SH",
    )
    broker = FakeBroker()
    strategy = {
        "strategy_id": "S003", "name": "成分资金流宽度早盘延续",
        "version": "v1", "release_id": "S003-v1",
        "release_hash": "c" * 64, "qualification": "PAPER_READY",
    }
    setup = intraday_setup_decision(strategy)
    AccountEngine(store, FakeAdvice(setup)).refresh_account("s003-v1")
    assert store.virtual_account("s003-v1")["cycle_target"] is None
    setup_execution = FutuExecution(
        store, broker, now=lambda: datetime(2026, 9, 2, 1, 30, 5, tzinfo=timezone.utc),
    )
    setup_execution.refresh_account()
    setup_execution.submit_pending(reconcile=False)
    setup_order = broker.value.orders[-1]
    broker.value = broker_snapshot(
        orders=(replace(
            setup_order, status="FILLED_ALL", cumulative_filled_quantity=1000,
            average_fill_price=1.67,
        ),),
        quantity=1000,
        symbol="510500.SH",
    )
    setup_execution.refresh_orders()
    assert store.virtual_account("s003-v1")["cycle_target"] == 1000

    plan = AdviceDecision(
        contract_version="advice.v5", decision_id="DEC-PLAN-BLOCKED",
        symbol="510500.SH", signal_date=date(2026, 9, 2),
        valid_session=date(2026, 9, 3), actual_quantity=1000,
        target_quantity=1000, cycle_target_quantity=1000, delta_quantity=0,
        action="ROTATE", strategy=strategy, signal_reference_price=1.70,
        execution_reference_price=1.70, data_cutoff=date(2026, 9, 2),
        order=None, orders=(), available_cash=90_000, fee_rate=0.0005,
        plan_mode="CORE_EVENT_INTRADAY_ROTATION",
        plan_legs=(
            PlanLegSpec(
                0, "ROTATION_ENTRY", "OPEN", time(9, 30), time(9, 35),
                None, None, OrderSpec("BUY", 1000, "LIMIT", 1.80, "DAY"),
            ),
            PlanLegSpec(
                1, "ROTATION_EXIT", "11:30_CLOSE", time(11, 29), time(11, 30),
                0, "FILLED_ALL", OrderSpec("SELL", 1000, "MARKET", 1.70, "DAY"),
            ),
        ),
    )
    AccountEngine(store, FakeAdvice(plan)).refresh_account("s003-v1")
    assert store.virtual_account("s003-v1")["cycle_target"] == 1000
    plan_intents = store.account_intents("s003-v1")[-2:]
    entry, exit_leg = plan_intents

    previous_evening = FutuExecution(
        store, broker, now=lambda: datetime(2026, 9, 2, 12, 51, tzinfo=timezone.utc),
    )
    previous_evening.refresh_account()
    previous_evening.submit_pending(reconcile=False)
    assert store.account_intent(entry["intent_id"])["status"] == "PENDING_SUBMIT"
    assert store.virtual_account("s003-v1")["health"] != "BLOCKED"

    open_execution = FutuExecution(
        store, broker, now=lambda: datetime(2026, 9, 3, 1, 30, 5, tzinfo=timezone.utc),
    )
    open_execution.refresh_account()
    open_execution.submit_pending(reconcile=False)
    assert [row.side for row in broker.placed] == ["BUY", "BUY"]

    after_deadline = FutuExecution(
        store, broker, now=lambda: datetime(2026, 9, 3, 1, 36, tzinfo=timezone.utc),
    )
    after_deadline.refresh_orders()
    assert broker.cancelled == [store.account_intent(entry["intent_id"])["channel_order_id"]]
    assert store.account_intent(entry["intent_id"])["status"] == "CANCELLING_ALL"
    assert store.account_intent(exit_leg["intent_id"])["status"] == "WAITING_DEPENDENCY"

    rotation_buy = broker.value.orders[-1]
    broker.value = replace(
        broker.value,
        orders=(
            broker.value.orders[0],
            replace(rotation_buy, status="CANCELLED_ALL"),
        ),
    )
    after_deadline.refresh_orders()
    noon_execution = FutuExecution(
        store, broker, now=lambda: datetime(2026, 9, 3, 3, 29, tzinfo=timezone.utc),
    )
    noon_execution.submit_pending(reconcile=False)
    assert [row.side for row in broker.placed] == ["BUY", "BUY"]
    assert store.account_intent(entry["intent_id"])["status"] == "CANCELLED_ALL"
    assert store.account_intent(exit_leg["intent_id"])["status"] == "EXPIRED"
    assert store.virtual_account("s003-v1")["health"] == "BLOCKED"
    assert len(store.query_audit_events(event_type="EXECUTION_PLAN_BLOCKED")) == 1
    store.close()


def test_existing_decision_id_is_preserved_when_same_decision_is_recomputed(tmp_path):
    store = PaperStore(tmp_path / "runtime.db")
    store.create_virtual_account(
        "s001-v1", "S001-v1模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S001", strategy_name_snapshot="综合基线策略",
        strategy_version="v1", release_hash="b" * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-01",
    )
    old_decision = decision()
    old_payload = asdict(old_decision)
    old_payload.pop("source_decision_id")
    store.save_account_decision("s001-v1", old_payload)

    accounts = AccountEngine(
        store, FakeAdvice(old_decision),
        now=lambda: datetime(2026, 9, 8, 6, 35, tzinfo=timezone.utc),
    )
    accounts.refresh_account("s001-v1")

    saved = store.account_decisions("s001-v1")
    assert len(saved) == 1
    assert saved[0]["decision_id"] == "DEC-ONE"
    assert store.virtual_account("s001-v1")["last_decision_id"] == "DEC-ONE"
    conflicting = dict(old_payload)
    conflicting["target_quantity"] = 100
    with pytest.raises(ValueError, match="idempotent"):
        store.save_account_decision("s001-v1", conflicting)
    store.close()


def test_operator_can_drive_one_account_decision_with_explicit_result_and_audit(tmp_path):
    store = PaperStore(tmp_path / "manual-decision.db")
    store.create_virtual_account(
        "s001-v1", "S001-v1模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S001", strategy_name_snapshot="综合基线策略",
        strategy_version="v1", release_hash="b" * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-01",
    )
    accounts = AccountEngine(store, FakeAdvice(decision()))
    coordinator = PteCoordinator(accounts, object())

    first = coordinator.drive_virtual_account_decision("s001-v1")
    repeated = coordinator.drive_virtual_account_decision("s001-v1")

    assert first == {
        "status": "DECISION_COMPLETED", "account_id": "s001-v1",
        "decision_id": first["decision_id"], "signal_date": "2026-09-01",
        "valid_session": "2026-09-02", "action": "WAIT",
        "target_quantity": 0, "execution_reference_price": 1.68,
        "reused_decision": False,
    }
    assert repeated["decision_id"] == first["decision_id"]
    assert repeated["reused_decision"] is True
    events = store.query_audit_events(
        event_type="ACCOUNT_DECISION_DRIVEN", account_id="s001-v1",
    )
    assert len(events) == 2
    assert all(row["actor_type"] == "OPERATOR" for row in events)
    store.set_virtual_health("s001-v1", "BLOCKED", "等待人工处理")
    with pytest.raises(AccountDecisionBlockedError, match="已阻塞"):
        coordinator.drive_virtual_account_decision("s001-v1")
    failed = store.query_audit_events(
        event_type="ACCOUNT_DECISION_DRIVE_FAILED", account_id="s001-v1",
    )
    assert len(failed) == 1
    assert failed[0]["outcome"] == "FAILURE"
    assert failed[0]["details"]["error_type"] == "AccountDecisionBlockedError"
    store.close()
