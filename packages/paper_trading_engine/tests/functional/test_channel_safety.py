from dataclasses import replace
from datetime import date, datetime
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from paper_trading_engine.audit import AuditRecorder
from paper_trading_engine.broker import (
    BrokerOrder,
    BrokerOrderRejectedError,
    BrokerPosition,
    BrokerSnapshot,
    OrderIntent,
    PaperTradingSafetyError,
)
from paper_trading_engine.futu_execution import ChannelReconciliationError, FutuExecution
from paper_trading_engine.futu_gateway import FutuGateway
from paper_trading_engine.coordinator import ReconnectableExecution
from paper_trading_engine.store import PaperStore
from pte_support import FakeBroker


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
    submitted = broker.value.orders
    filled = tuple(
        replace(
            order, status="FILLED_ALL", cumulative_filled_quantity=1000,
            average_fill_price=1.67 if order.symbol == "588080.SH" else 7.49,
        )
        for order in submitted
    )
    broker.value = BrokerSnapshot(
        broker.value.account,
        (BrokerPosition("588080.SH", 1000), BrokerPosition("510500.SH", 1000)),
        filled,
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
    assert len(store.query_audit_events(event_type="ORDER_REJECTED")) == 1

    store.set_virtual_health("s001-v1", "OK")
    second = store.create_account_intent(
        account_id="s001-v1", decision_id="DEC-ATOMIC", order_sequence=0,
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
