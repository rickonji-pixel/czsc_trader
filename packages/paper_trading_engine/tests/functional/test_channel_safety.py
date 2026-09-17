from dataclasses import replace
from datetime import date, datetime
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from types import SimpleNamespace

import pytest

from paper_trading_engine.audit import AuditRecorder
from paper_trading_engine.account_engine import AccountEngine
from paper_trading_engine.broker import (
    BrokerAccount,
    BrokerOrder,
    BrokerOrderRejectedError,
    BrokerPosition,
    BrokerSnapshot,
    OrderIntent,
    PaperTradingSafetyError,
)
from paper_trading_engine.futu_execution import ChannelReconciliationError, FutuExecution
from paper_trading_engine.futu_gateway import FutuGateway, FutuGatewayError
from paper_trading_engine.coordinator import ReconnectableExecution
from paper_trading_engine.store import PaperStore
from paper_trading_engine.contracts import OrderSpec
from pte_support import FakeAdvice, FakeBroker, decision


def test_ft_pte03_estimated_fees_reconcile_to_futu_cash_exactly_once(tmp_path):
    store = PaperStore(tmp_path / "fee-reconciliation.db")
    store.create_virtual_account(
        "s001-v2", "S001-v2模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S001", strategy_name_snapshot="综合基线策略",
        strategy_version="v2", release_hash="b" * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-02",
    )
    buy = store.create_account_intent(
        account_id="s001-v2", decision_id="DEC-BUY", order_sequence=0,
        symbol="588080.SH", side="BUY", quantity=1000,
        limit_price="1.680", valid_session="2026-09-04", fee_rate="0.0005",
    )
    broker = FakeBroker()
    execution = FutuExecution(
        store, broker, now=lambda: datetime.fromisoformat("2026-09-04T10:00:00+08:00"),
    )
    execution.submit_pending()
    filled_buy = replace(
        broker.value.orders[0], status="FILLED_ALL",
        cumulative_filled_quantity=1000, average_fill_price=1.67,
    )
    broker.value = BrokerSnapshot(
        BrokerAccount("SIMULATE", "CN", 998_326.66, 999_996.66, 0),
        (BrokerPosition("588080.SH", 1000),), (filled_buy,),
    )
    execution.refresh_orders()
    bought = store.virtual_account("s001-v2")
    assert float(bought["cash"]) == pytest.approx(98_326.66)
    assert float(bought["average_cost"]) == pytest.approx(1.67334)
    assert store.get_setting("futu_cash_reconciliation_status") == "OK"
    assert len(store.query_audit_events(event_type="BROKER_FEE_RECONCILED")) == 1

    # Repeating the same broker snapshot is idempotent.
    execution.refresh_orders()
    assert float(store.virtual_account("s001-v2")["cash"]) == pytest.approx(98_326.66)
    assert len(store.query_audit_events(event_type="BROKER_FEE_RECONCILED")) == 1

    store.create_account_intent(
        account_id="s001-v2", decision_id="DEC-SELL", order_sequence=0,
        symbol="588080.SH", side="SELL", quantity=1000,
        limit_price="1.600", valid_session="2026-09-04", fee_rate="0.0005",
    )
    execution.submit_pending()
    sell_order = next(row for row in broker.value.orders if row.side == "SELL")
    filled_sell = replace(
        sell_order, status="FILLED_ALL",
        cumulative_filled_quantity=1000, average_fill_price=1.60,
    )
    broker.value = BrokerSnapshot(
        BrokerAccount("SIMULATE", "CN", 999_923.46, 999_923.46, 0),
        (), (filled_buy, filled_sell),
    )
    execution.refresh_orders()
    closed = store.virtual_account("s001-v2")
    assert float(closed["cash"]) == pytest.approx(99_923.46)
    assert float(closed["total_assets"]) == pytest.approx(99_923.46)
    assert float(closed["realized_pnl"]) == pytest.approx(-76.54)
    assert store.account_invariant_violations() == []
    assert len(store.query_audit_events(event_type="BROKER_FEE_RECONCILED")) == 2
    assert store.account_intent(buy["intent_id"])["status"] == "FILLED_ALL"
    store.close()

    # Legacy four-decimal cost bases are repaired once after an account is flat.
    with sqlite3.connect(tmp_path / "fee-reconciliation.db") as connection:
        connection.execute(
            "UPDATE virtual_accounts SET realized_pnl='-75.0000' WHERE account_id='s001-v2'"
        )
        connection.execute(
            "UPDATE fills SET realized_pnl='-75.0000' WHERE account_id='s001-v2' AND side='SELL'"
        )
    migrated = PaperStore(tmp_path / "fee-reconciliation.db")
    assert float(migrated.virtual_account("s001-v2")["realized_pnl"]) == pytest.approx(-76.54)
    assert sum(float(row["realized_pnl"]) for row in migrated.account_fills("s001-v2")) == (
        pytest.approx(-76.54)
    )
    migration_count = len(migrated.query_audit_events(event_type="ACCOUNT_EXECUTION_MIGRATED"))
    migrated.close()
    reopened = PaperStore(tmp_path / "fee-reconciliation.db")
    assert len(reopened.query_audit_events(event_type="ACCOUNT_EXECUTION_MIGRATED")) == (
        migration_count
    )
    reopened.close()


def test_ft_pte03_multiple_accounts_share_only_safe_futu_channel(tmp_path):
    store = PaperStore(tmp_path / "shared-futu.db")
    for account_id, strategy, version, marker, symbol in (
        ("s001-v1", "S001", "v1", "a", "588080.SH"),
        ("s001-v2", "S001", "v2", "b", "588080.SH"),
        ("s002-v1", "S002", "v1", "c", "510500.SH"),
    ):
        store.create_virtual_account(
            account_id, f"{account_id}模拟账户", "legacy", marker * 64, 100_000,
            strategy_id=strategy, strategy_name_snapshot="测试策略",
            strategy_version=version, release_hash=marker * 64,
            qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-02",
            symbol=symbol,
        )
    first_intent = store.create_account_intent(
        account_id="s001-v2", decision_id="DEC-2", order_sequence=0,
        symbol="588080.SH", side="BUY", quantity=1000,
        limit_price="1.680", valid_session="2026-09-04",
    )
    store.create_account_intent(
        account_id="s002-v1", decision_id="DEC-3", order_sequence=0,
        symbol="510500.SH", side="BUY", quantity=1000,
        limit_price="7.500", valid_session="2026-09-04",
    )
    assert first_intent["intent_id"].startswith("PTE-")
    assert len(first_intent["intent_id"]) <= 24
    broker = FakeBroker()
    execution = FutuExecution(store, broker, symbol="588080.SH", today=lambda: date(2026, 9, 4))
    execution.refresh_account()
    execution.submit_pending()
    assert len(broker.value.orders) == 1
    first_order = replace(
        broker.value.orders[0], status="FILLED_ALL", cumulative_filled_quantity=1000,
        average_fill_price=1.67,
    )
    broker.value = BrokerSnapshot(
        broker.value.account,
        (BrokerPosition("588080.SH", 1000),), (first_order,),
    )
    execution.refresh_orders()
    execution.submit_pending()
    assert len(broker.value.orders) == 2
    second_order = replace(
        broker.value.orders[1], status="FILLED_ALL", cumulative_filled_quantity=1000,
        average_fill_price=7.49,
    )
    submitted = (first_order, second_order)
    broker.value = BrokerSnapshot(
        broker.value.account,
        (BrokerPosition("588080.SH", 1000), BrokerPosition("510500.SH", 1000)),
        submitted,
    )
    execution.refresh_orders()
    assert store.virtual_account("s001-v1")["quantity"] == 0
    assert store.virtual_account("s001-v2")["quantity"] == 1000
    assert store.virtual_account("s002-v1")["quantity"] == 1000

    foreign = replace(
        submitted[0], channel_order_id="9999", quantity=100,
        status="SUBMITTED", cumulative_filled_quantity=0, remark="MANUAL",
    )
    broker.value = BrokerSnapshot(
        broker.value.account,
        (BrokerPosition("588080.SH", 1000), BrokerPosition("510500.SH", 1000)),
        (foreign,),
    )
    with pytest.raises(ChannelReconciliationError, match="无法归属"):
        execution.refresh_orders()
    assert store.get_setting("channel_reconciliation_status") == "BLOCKED"
    store.close()

    class TradeContext:
        def __init__(self): self.place_calls, self.modify_calls = [], []
        def get_acc_list(self): return 0, [{"acc_id": 77, "trd_env": "SIMULATE", "trd_market": "CN"}]
        def accinfo_query(self, **kwargs): return 0, [{"cash": 900_000, "total_assets": 1_000_000, "frozen_cash": 0}]
        def position_list_query(self, **kwargs): return 0, []
        def order_list_query(self, **kwargs): return 0, []
        def place_order(self, **kwargs):
            self.place_calls.append(kwargs)
            return 0, [{"order_id": "124", "code": kwargs["code"], "trd_side": kwargs["trd_side"], "qty": kwargs["qty"], "price": kwargs["price"], "order_type": kwargs["order_type"], "order_status": "SUBMITTING", "dealt_qty": 0, "dealt_avg_price": 0, "remark": kwargs["remark"]}]
        def modify_order(self, **kwargs):
            self.modify_calls.append(kwargs)
            return 0, []
        def close(self):
            pass

    sdk = SimpleNamespace(
        RET_OK=0, TrdEnv=SimpleNamespace(SIMULATE="SIMULATE"),
        TrdMarket=SimpleNamespace(CN="CN"), TrdSide=SimpleNamespace(BUY="BUY", SELL="SELL"),
        OrderType=SimpleNamespace(NORMAL="NORMAL", MARKET="MARKET"),
        TimeInForce=SimpleNamespace(DAY="DAY"),
        ModifyOrderOp=SimpleNamespace(CANCEL="CANCEL"),
        SysConfig=SimpleNamespace(enable_console_log=lambda enabled: None),
    )
    audit_store = PaperStore(tmp_path / "gateway.db")
    trade = TradeContext()
    gateway = FutuGateway(
        symbol="588080.SH", sdk=sdk, trade_context=trade,
        audit=AuditRecorder(audit_store),
    )
    snapshot = gateway.account_snapshot()
    assert snapshot.account.environment == "SIMULATE"
    gateway.place_order(OrderIntent("PTE-s001-v1-X", "DEC-X", "588080.SH", "BUY", 1000, 1.68))
    assert trade.place_calls[0]["trd_env"] == "SIMULATE"
    assert trade.place_calls[0]["adjust_limit"] == 0
    gateway.place_order(OrderIntent("PTE-s002-v1-X", "DEC-Y", "510500.SH", "BUY", 1000, 7.5))
    assert trade.place_calls[1]["code"] == "SH.510500"
    gateway.place_order(OrderIntent(
        "PTE-s001-v2-X", "DEC-M", "588080.SH", "SELL", 1000, 1.6,
        order_type="MARKET",
    ))
    assert trade.place_calls[2]["order_type"] == "MARKET"
    with pytest.raises(PaperTradingSafetyError, match="China-market"):
        gateway.place_order(OrderIntent("PTE-X", "DEC-Z", "AAPL.US", "BUY", 1000, 1.0))
    audit_store.close()


def test_ft_pte03_serializes_futu_simulated_cn_fee_attribution_per_order(tmp_path):
    store = PaperStore(tmp_path / "serialized-fees.db")
    for account_id, symbol in (("s001-v1", "588080.SH"), ("s002-v1", "510500.SH")):
        store.create_virtual_account(
            account_id, f"{account_id}模拟账户", "legacy", account_id[-1] * 64, 100_000,
            strategy_id=account_id[:4].upper(), strategy_name_snapshot="测试策略",
            strategy_version="v1", release_hash=account_id[-1] * 64,
            qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-02",
            symbol=symbol,
        )
    store.create_account_intent(
        account_id="s001-v1", decision_id="DEC-1", order_sequence=0,
        symbol="588080.SH", side="BUY", quantity=1000, limit_price="1.680",
        valid_session="2026-09-04", fee_rate="0.0005",
    )
    store.create_account_intent(
        account_id="s002-v1", decision_id="DEC-2", order_sequence=0,
        symbol="510500.SH", side="BUY", quantity=1000, limit_price="7.500",
        valid_session="2026-09-04", fee_rate="0.0005",
    )
    broker = FakeBroker()
    execution = FutuExecution(
        store, broker, now=lambda: datetime.fromisoformat("2026-09-04T10:00:00+08:00"),
    )

    execution.submit_pending()
    assert len(broker.placed) == 1
    assert store.account_order("1001")["pte_fee_attribution"]["status"] == "PENDING"

    first = replace(
        broker.value.orders[0], status="FILLED_ALL", cumulative_filled_quantity=1000,
        average_fill_price=1.67,
    )
    broker.value = BrokerSnapshot(
        BrokerAccount("SIMULATE", "CN", 998_329.00, 999_999.00, 0),
        (BrokerPosition("588080.SH", 1000),), (first,),
    )
    execution.refresh_orders()
    assert float(store.virtual_account("s001-v1")["cash"]) == pytest.approx(98_329.00)

    execution.submit_pending()
    assert len(broker.placed) == 2
    second = replace(
        broker.value.orders[1], status="FILLED_ALL", cumulative_filled_quantity=1000,
        average_fill_price=7.49,
    )
    broker.value = BrokerSnapshot(
        BrokerAccount("SIMULATE", "CN", 990_834.50, 1_000_000.00, 0),
        (BrokerPosition("588080.SH", 1000), BrokerPosition("510500.SH", 1000)),
        (first, second),
    )
    execution.refresh_orders()

    assert float(store.virtual_account("s002-v1")["cash"]) == pytest.approx(92_505.50)
    assert store.get_setting("futu_cash_reconciliation_status") == "OK"
    assert [
        row["pte_fee_attribution"]["status"] for row in store.account_orders()
    ] == ["RECONCILED", "RECONCILED"]
    events = store.query_audit_events(event_type="BROKER_FEE_RECONCILED")
    assert {event["account_id"] for event in events} == {"s001-v1", "s002-v1"}
    assert store.account_invariant_violations() == []
    store.close()


def test_ft_pte03_fee_attribution_rejects_live_futu_snapshot(tmp_path):
    store = PaperStore(tmp_path / "live-futu-rejected.db")
    broker = FakeBroker()
    broker.value = BrokerSnapshot(BrokerAccount("REAL", "CN", 1_000_000, 1_000_000, 0), (), ())
    execution = FutuExecution(store, broker)

    with pytest.raises(PaperTradingSafetyError, match="environment must be SIMULATE"):
        execution.refresh_account()
    store.close()


def test_ft_pte03_executor_rejects_undefined_channel_environment_market_scope(tmp_path):
    class OtherChannelBroker(FakeBroker):
        channel_id = "other"

    store = PaperStore(tmp_path / "other-channel.db")
    broker = OtherChannelBroker()
    execution = FutuExecution(store, broker)
    with pytest.raises(PaperTradingSafetyError, match="channel must be futu"):
        execution.refresh_account()

    broker = FakeBroker()
    broker.value = BrokerSnapshot(BrokerAccount("SIMULATE", "US", 1_000_000, 1_000_000, 0), (), ())
    execution = FutuExecution(store, broker)
    with pytest.raises(PaperTradingSafetyError, match="market must be CN"):
        execution.refresh_account()

    store.close()


def test_ft_pte03_gateway_rejects_account_without_explicit_cn_market(tmp_path):
    class TradeContext:
        def get_acc_list(self):
            return 0, [{"acc_id": 77, "trd_env": "SIMULATE"}]

        def close(self):
            pass

    sdk = SimpleNamespace(
        RET_OK=0, TrdEnv=SimpleNamespace(SIMULATE="SIMULATE"),
        TrdMarket=SimpleNamespace(CN="CN"),
        SysConfig=SimpleNamespace(enable_console_log=lambda enabled: None),
    )
    store = PaperStore(tmp_path / "missing-market.db")
    gateway = FutuGateway(sdk=sdk, trade_context=TradeContext(), audit=AuditRecorder(store))

    with pytest.raises(FutuGatewayError, match="expected one CN SIMULATE account"):
        gateway.account_snapshot()
    store.close()


def test_ft_pte03_failed_or_cancelled_buy_releases_reserved_cash(tmp_path):
    store = PaperStore(tmp_path / "release-cash.db")
    store.create_virtual_account(
        "s001-v1", "S001-v1模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S001", strategy_name_snapshot="综合基线策略",
        strategy_version="v1", release_hash="a" * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-02",
    )
    intent = store.create_account_intent(
        account_id="s001-v1", decision_id="DEC-FAIL", order_sequence=0,
        symbol="588080.SH", side="BUY", quantity=1000,
        limit_price="1.680", valid_session="2026-09-04",
    )
    assert float(store.virtual_account("s001-v1")["frozen_cash"]) > 0

    store.release_account_intent(intent["intent_id"], "SUBMISSION_FAILED")
    account = store.virtual_account("s001-v1")
    assert float(account["cash"]) == 100_000
    assert float(account["frozen_cash"]) == 0
    assert store.account_intent(intent["intent_id"])["status"] == "SUBMISSION_FAILED"

    second = store.create_account_intent(
        account_id="s001-v1", decision_id="DEC-CANCEL", order_sequence=0,
        symbol="588080.SH", side="BUY", quantity=1000,
        limit_price="1.680", valid_session="2026-09-04",
    )
    assert store.claim_account_intent(second["intent_id"])
    store.bind_channel_order(second["intent_id"], "2001", {
        "channel_order_id": "2001", "symbol": "588080.SH", "side": "BUY",
        "quantity": 1000, "limit_price": 1.68, "status": "SUBMITTED",
        "cumulative_filled_quantity": 0, "average_fill_price": 0,
        "remark": second["intent_id"],
    })
    store.update_channel_order_report("2001", {
        "channel_order_id": "2001", "symbol": "588080.SH", "side": "BUY",
        "quantity": 1000, "limit_price": 1.68, "status": "CANCELLED_ALL",
        "cumulative_filled_quantity": 0, "average_fill_price": 0,
        "remark": second["intent_id"],
    })
    account = store.virtual_account("s001-v1")
    assert float(account["cash"]) == 100_000
    assert float(account["frozen_cash"]) == 0
    store.close()


def test_ft_pte03_channel_and_account_pause_block_pending_submission(tmp_path):
    store = PaperStore(tmp_path / "pause.db")
    store.create_virtual_account(
        "s001-v1", "S001-v1模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S001", strategy_name_snapshot="综合基线策略",
        strategy_version="v1", release_hash="a" * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-02",
    )
    store.create_account_intent(
        account_id="s001-v1", decision_id="DEC-PAUSE", order_sequence=0,
        symbol="588080.SH", side="BUY", quantity=1000,
        limit_price="1.680", valid_session="2026-09-04",
    )
    broker = FakeBroker()
    execution = FutuExecution(store, broker, symbol="588080.SH", today=lambda: date(2026, 9, 4))
    execution.refresh_account()
    execution.pause()
    execution.submit_pending()
    assert broker.placed == []
    execution.resume()
    store.set_virtual_paused("s001-v1", True)
    execution.submit_pending()
    assert broker.placed == []
    store.close()


def test_ft_pte03_uncertain_submission_keeps_reservation_and_blocks_account(tmp_path):
    class FailingBroker(FakeBroker):
        def place_order(self, intent):
            raise TimeoutError("broker response lost")

    store = PaperStore(tmp_path / "uncertain.db")
    store.create_virtual_account(
        "s001-v1", "S001-v1模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S001", strategy_name_snapshot="综合基线策略",
        strategy_version="v1", release_hash="a" * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-02",
    )
    intent = store.create_account_intent(
        account_id="s001-v1", decision_id="DEC-UNCERTAIN", order_sequence=0,
        symbol="588080.SH", side="BUY", quantity=1000,
        limit_price="1.680", valid_session="2026-09-04",
    )
    execution = FutuExecution(
        store, FailingBroker(), symbol="588080.SH", today=lambda: date(2026, 9, 4)
    )
    with pytest.raises(TimeoutError, match="response lost"):
        execution.submit_pending()
    account = store.virtual_account("s001-v1")
    assert float(account["frozen_cash"]) > 0
    assert account["health"] == "BLOCKED"
    assert store.account_intent(intent["intent_id"])["status"] == "SUBMISSION_UNCERTAIN"
    store.close()


def test_ft_pte03_explicit_rejection_releases_cash_and_duplicate_submit_is_atomic(tmp_path):
    class RejectingBroker(FakeBroker):
        def place_order(self, intent):
            self.placed.append(intent)
            raise BrokerOrderRejectedError("报单价格不在涨跌停区间")

    store = PaperStore(tmp_path / "reject.db")
    store.create_virtual_account(
        "s001-v1", "S001-v1模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S001", strategy_name_snapshot="综合基线策略",
        strategy_version="v1", release_hash="a" * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-02",
    )
    rejected = store.create_account_intent(
        account_id="s001-v1", decision_id="DEC-REJECT", order_sequence=0,
        symbol="588080.SH", side="BUY", quantity=1000,
        limit_price="1.680", valid_session="2026-09-04",
    )
    broker = RejectingBroker()
    execution = FutuExecution(
        store, broker, now=lambda: datetime.fromisoformat("2026-09-04T10:00:00+08:00")
    )
    execution.submit_pending()
    assert store.account_intent(rejected["intent_id"])["status"] == "REJECTED"
    assert float(store.virtual_account("s001-v1")["frozen_cash"]) == 0
    assert store.account_invariant_violations() == []
    assert len(store.query_audit_events(event_type="ORDER_REJECTED")) == 1

    store.set_virtual_health("s001-v1", "OK")
    second = store.create_account_intent(
        account_id="s001-v1", decision_id="DEC-REJECT", order_sequence=1,
        symbol="588080.SH", side="BUY", quantity=1000,
        limit_price="1.680", valid_session="2026-09-04",
    )
    safe_broker = FakeBroker()
    first = FutuExecution(
        store, safe_broker, now=lambda: datetime.fromisoformat("2026-09-04T10:00:00+08:00")
    )
    second_runner = FutuExecution(
        store, safe_broker, now=lambda: datetime.fromisoformat("2026-09-04T10:00:00+08:00")
    )
    first.refresh_account()
    second_runner.refresh_account()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda runner: runner.submit_pending(reconcile=False), (first, second_runner)))
    assert len(safe_broker.placed) == 1
    assert store.account_intent(second["intent_id"])["status"] == "SUBMITTED"
    filled = replace(
        safe_broker.value.orders[0], status="FILLED_ALL",
        cumulative_filled_quantity=1000, average_fill_price=1.67,
    )
    safe_broker.value = BrokerSnapshot(
        safe_broker.value.account, (BrokerPosition("588080.SH", 1000),), (filled,),
    )
    first.refresh_orders()
    store.close()

    # A pre-v2 failed attempt followed by a completed retry remains resolved on migration.
    with sqlite3.connect(tmp_path / "reject.db") as connection:
        connection.execute(
            "UPDATE intents SET attention_required=0,attention_reason=NULL,"
            "resolved_at=NULL,resolution_note=NULL WHERE intent_id=?",
            (rejected["intent_id"],),
        )
    migrated = PaperStore(tmp_path / "reject.db")
    migrated_rejection = migrated.account_intent(rejected["intent_id"])
    assert migrated_rejection["attention_required"] is False
    assert second["intent_id"] in migrated_rejection["resolution_note"]
    assert migrated.attention_account_intents("s001-v1") == []
    migration_count = len(migrated.query_audit_events(
        event_type="ACCOUNT_EXECUTION_MIGRATED",
    ))
    migrated.close()
    reopened = PaperStore(tmp_path / "reject.db")
    assert len(reopened.query_audit_events(
        event_type="ACCOUNT_EXECUTION_MIGRATED",
    )) == migration_count
    reopened.close()


def test_ft_pte03_incomplete_and_unknown_orders_never_silently_recover(tmp_path):
    partial_store = PaperStore(tmp_path / "partial-cancel.db")
    partial_store.create_virtual_account(
        "s001-v1", "S001-v1模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S001", strategy_name_snapshot="综合基线策略",
        strategy_version="v1", release_hash="b" * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-01",
    )
    partial = partial_store.create_account_intent(
        account_id="s001-v1", decision_id="DEC-PARTIAL", order_sequence=0,
        symbol="588080.SH", side="BUY", quantity=1000,
        limit_price="1.680", valid_session="2026-09-02",
    )
    partial_broker = FakeBroker()
    partial_execution = FutuExecution(
        partial_store, partial_broker, today=lambda: date(2026, 9, 2),
    )
    partial_execution.submit_pending()
    cancelled = replace(
        partial_broker.value.orders[0], status="CANCELLED_PART",
        cumulative_filled_quantity=400, average_fill_price=1.67,
    )
    partial_broker.value = BrokerSnapshot(
        partial_broker.value.account, (BrokerPosition("588080.SH", 400),), (cancelled,),
    )
    partial_execution.refresh_orders()
    account = partial_store.virtual_account("s001-v1")
    intent = partial_store.account_intent(partial["intent_id"])
    assert intent["status"] == "CANCELLED_PART"
    assert intent["attention_required"] is True
    assert account["health"] == "BLOCKED"
    assert float(account["frozen_cash"]) == 0
    assert partial_store.account_invariant_violations() == []
    partial_execution.acknowledge_execution_gap(
        "s001-v1", partial["intent_id"], "已确认部分成交，保留实际400股持仓",
    )
    assert partial_store.virtual_account("s001-v1")["health"] == "OK"
    assert partial_store.attention_account_intents("s001-v1") == []
    partial_store.create_account_intent(
        account_id="s001-v1", decision_id="DEC-CLOSE", order_sequence=0,
        symbol="588080.SH", side="SELL", quantity=400,
        limit_price="1.600", valid_session="2026-09-02", order_type="MARKET",
    )
    partial_execution.submit_pending()
    sell_order = next(order for order in partial_broker.value.orders if order.side == "SELL")
    sold = replace(
        sell_order, status="FILLED_ALL", cumulative_filled_quantity=400,
        average_fill_price=1.60,
    )
    partial_broker.value = BrokerSnapshot(
        partial_broker.value.account, (), (cancelled, sold),
    )
    partial_execution.refresh_orders()
    closed = partial_store.virtual_account("s001-v1")
    assert closed["quantity"] == 0
    assert float(closed["cash"]) - 100_000 == pytest.approx(float(closed["realized_pnl"]))
    assert partial_store.account_invariant_violations() == []
    partial_store.close()

    timeout_store = PaperStore(tmp_path / "timeout.db")
    timeout_store.create_virtual_account(
        "s001-v1", "S001-v1模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S001", strategy_name_snapshot="综合基线策略",
        strategy_version="v1", release_hash="b" * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-01",
    )
    timeout = timeout_store.create_account_intent(
        account_id="s001-v1", decision_id="DEC-TIMEOUT", order_sequence=0,
        symbol="588080.SH", side="BUY", quantity=1000,
        limit_price="1.680", valid_session="2026-09-02",
    )
    timeout_broker = FakeBroker()
    timeout_execution = FutuExecution(
        timeout_store, timeout_broker, today=lambda: date(2026, 9, 2),
    )
    timeout_execution.submit_pending()
    unknown = replace(timeout_broker.value.orders[0], status="TIMEOUT")
    timeout_broker.value = replace(
        timeout_broker.value,
        orders=(replace(unknown, status="FUTURE_UNKNOWN_STATUS"),),
    )
    with pytest.raises(ChannelReconciliationError, match="不一致"):
        timeout_execution.refresh_orders()
    timeout_broker.value = replace(timeout_broker.value, orders=(unknown,))
    timeout_execution.refresh_orders()
    assert timeout_store.virtual_account("s001-v1")["health"] == "BLOCKED"
    assert [row["intent_id"] for row in timeout_store.unresolved_account_intents()] == [
        timeout["intent_id"]
    ]
    filled = replace(
        unknown, status="FILLED_ALL", cumulative_filled_quantity=1000,
        average_fill_price=1.67,
    )
    timeout_broker.value = BrokerSnapshot(
        timeout_broker.value.account, (BrokerPosition("588080.SH", 1000),), (filled,),
    )
    timeout_execution.refresh_orders()
    assert timeout_store.virtual_account("s001-v1")["health"] == "OK"
    assert timeout_store.unresolved_account_intents() == []
    assert timeout_store.account_invariant_violations() == []
    timeout_store.close()


def test_ft_pte03_rejected_decision_remains_blocked_until_operator_review(tmp_path):
    class RejectingBroker(FakeBroker):
        def place_order(self, intent):
            self.placed.append(intent)
            raise BrokerOrderRejectedError("模拟拒单")

    store = PaperStore(tmp_path / "persistent-rejection.db")
    store.create_virtual_account(
        "s001-v1", "S001-v1模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S001", strategy_name_snapshot="综合基线策略",
        strategy_version="v1", release_hash="b" * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-01",
    )
    accounts = AccountEngine(
        store, FakeAdvice(decision(OrderSpec("BUY", 1000, "LIMIT", 1.68, "DAY"))),
        now=lambda: datetime.fromisoformat("2026-09-01T20:30:00+08:00"),
    )
    accounts.refresh_account("s001-v1")
    execution = FutuExecution(
        store, RejectingBroker(), now=lambda: datetime.fromisoformat("2026-09-02T10:00:00+08:00"),
    )
    execution.submit_pending()
    accounts.refresh_account("s001-v1")
    assert store.virtual_account("s001-v1")["health"] == "BLOCKED"
    assert len(store.account_intents("s001-v1")) == 1
    assert len(store.attention_account_intents("s001-v1")) == 1
    store.close()


def test_ft_pte03_missing_broker_order_and_overfill_block_without_mutating_ledger(tmp_path):
    store = PaperStore(tmp_path / "reconcile.db")
    store.create_virtual_account(
        "s001-v1", "S001-v1模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S001", strategy_name_snapshot="综合基线策略",
        strategy_version="v1", release_hash="a" * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-02",
    )
    intent = store.create_account_intent(
        account_id="s001-v1", decision_id="DEC-MISSING", order_sequence=0,
        symbol="588080.SH", side="BUY", quantity=1000,
        limit_price="1.680", valid_session="2026-09-04",
    )
    assert store.claim_account_intent(intent["intent_id"])
    order = BrokerOrder(
        "3001", "588080.SH", "BUY", 1000, 1.68,
        "SUBMITTED", 0, 0, intent["intent_id"],
    )
    store.bind_channel_order(intent["intent_id"], "3001", order.__dict__)
    account_before = store.virtual_account("s001-v1")
    with pytest.raises(ValueError, match="exceeds intent quantity"):
        store.apply_fill_increment(
            "3001", cumulative_quantity=1100, average_price=1.67,
            occurred_at="2026-09-04T10:01:00+08:00",
        )
    account_after = store.virtual_account("s001-v1")
    assert account_after["cash"] == account_before["cash"]
    assert account_after["frozen_cash"] == account_before["frozen_cash"]
    assert account_after["quantity"] == 0
    assert store.account_fills("s001-v1") == []

    execution = FutuExecution(
        store, FakeBroker(), now=lambda: datetime.fromisoformat("2026-09-04T10:00:00+08:00")
    )
    with pytest.raises(ChannelReconciliationError, match="不存在"):
        execution.refresh_orders()
    assert store.get_setting("channel_reconciliation_status") == "BLOCKED"
    assert store.virtual_account("s001-v1")["health"] == "BLOCKED"
    store.close()


def test_ft_pte03_opend_can_reconnect_without_restarting_pte(tmp_path):
    store = PaperStore(tmp_path / "reconnect.db")
    attempts = 0

    def factory():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("OpenD unavailable")
        return FutuExecution(store, FakeBroker())

    execution = ReconnectableExecution(store, "588080.SH", factory)
    with pytest.raises(ConnectionError, match="OpenD unavailable"):
        execution.refresh_account()
    assert execution.status()["reconciliation_status"] == "UNAVAILABLE"
    status = execution.refresh_account()
    assert status["account"]["environment"] == "SIMULATE"
    assert execution.status()["reconciliation_status"] != "UNAVAILABLE"
    assert attempts == 2
    execution.close()

    recovered_store = PaperStore(tmp_path / "reconnect-established.db")
    replacement = FutuExecution(recovered_store, FakeBroker())

    class BrokenBroker:
        def __init__(self): self.closed = False
        def close(self): self.closed = True

    class BrokenExecution:
        def __init__(self): self.broker = BrokenBroker()
        def refresh_account(self): raise FutuGatewayError("connection permanently lost")
        def status(self): return {"reconciliation_status": "UNAVAILABLE"}

    broken = BrokenExecution()
    established = ReconnectableExecution(
        recovered_store, "588080.SH", lambda: replacement, initial=broken,
    )
    with pytest.raises(FutuGatewayError, match="permanently lost"):
        established.refresh_account()
    assert broken.broker.closed is True
    assert established.refresh_account()["account"]["environment"] == "SIMULATE"
    established.close()
