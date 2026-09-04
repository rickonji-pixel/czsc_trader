from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from paper_trading_engine.audit import AuditRecorder
from paper_trading_engine.broker import OrderIntent, PaperTradingSafetyError
from paper_trading_engine.futu_execution import ChannelReconciliationError, FutuExecution
from paper_trading_engine.futu_gateway import FutuGateway
from paper_trading_engine.store import PaperStore
from pte_support import FakeBroker, broker_snapshot


def test_ft_pte03_multiple_accounts_share_only_safe_futu_channel(tmp_path):
    store = PaperStore(tmp_path / "shared-futu.db")
    for account_id, version, marker in (("s001-v1", "v1", "a"), ("s001-v2", "v2", "b")):
        store.create_virtual_account(
            account_id, f"{account_id}模拟账户", "legacy", marker * 64, 100_000,
            strategy_id="S001", strategy_name_snapshot="综合基线策略",
            strategy_version=version, release_hash=marker * 64,
            qualification_snapshot="PAPER_READY",
        )
    intent = store.create_account_intent(
        account_id="s001-v2", decision_id="DEC-2", order_sequence=0,
        symbol="588080.SH", side="BUY", quantity=1000,
        limit_price="1.680", valid_session="2026-09-04",
    )
    assert intent["intent_id"].startswith("PTE-")
    assert len(intent["intent_id"]) <= 24
    broker = FakeBroker()
    execution = FutuExecution(store, broker, symbol="588080.SH", today=lambda: date(2026, 9, 4))
    execution.refresh_account()
    execution.submit_pending()
    submitted = broker.value.orders[0]
    broker.value = broker_snapshot(
        orders=(replace(
            submitted, status="FILLED_ALL", cumulative_filled_quantity=1000,
            average_fill_price=1.67,
        ),), quantity=1000,
    )
    execution.refresh_orders()
    assert store.virtual_account("s001-v1")["quantity"] == 0
    assert store.virtual_account("s001-v2")["quantity"] == 1000

    foreign = replace(
        submitted, channel_order_id="9999", quantity=100,
        status="SUBMITTED", cumulative_filled_quantity=0, remark="MANUAL",
    )
    broker.value = broker_snapshot(orders=(foreign,), quantity=1000)
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
            return 0, [{"order_id": "124", "code": kwargs["code"], "trd_side": kwargs["trd_side"], "qty": kwargs["qty"], "price": kwargs["price"], "order_status": "SUBMITTING", "dealt_qty": 0, "dealt_avg_price": 0, "remark": kwargs["remark"]}]
        def modify_order(self, **kwargs):
            self.modify_calls.append(kwargs)
            return 0, []
        def close(self):
            pass

    class QuoteContext:
        def get_stock_quote(self, code_list): return -1, "no entitlement"
        def close(self): pass

    sdk = SimpleNamespace(
        RET_OK=0, TrdEnv=SimpleNamespace(SIMULATE="SIMULATE"),
        TrdMarket=SimpleNamespace(CN="CN"), TrdSide=SimpleNamespace(BUY="BUY", SELL="SELL"),
        OrderType=SimpleNamespace(NORMAL="NORMAL"), TimeInForce=SimpleNamespace(DAY="DAY"),
        ModifyOrderOp=SimpleNamespace(CANCEL="CANCEL"),
        SysConfig=SimpleNamespace(enable_console_log=lambda enabled: None),
    )
    audit_store = PaperStore(tmp_path / "gateway.db")
    trade = TradeContext()
    gateway = FutuGateway(
        symbol="588080.SH", sdk=sdk, trade_context=trade, quote_context=QuoteContext(),
        audit=AuditRecorder(audit_store),
    )
    gateway.place_order(OrderIntent("PTE-s001-v1-X", "DEC-X", "588080.SH", "BUY", 1000, 1.68))
    assert trade.place_calls[0]["trd_env"] == "SIMULATE"
    assert trade.place_calls[0]["adjust_limit"] == 0
    with pytest.raises(PaperTradingSafetyError, match="whitelist"):
        gateway.place_order(OrderIntent("PTE-X", "DEC-X", "159352.SZ", "BUY", 1000, 1.0))
    audit_store.close()


def test_ft_pte03_failed_or_cancelled_buy_releases_reserved_cash(tmp_path):
    store = PaperStore(tmp_path / "release-cash.db")
    store.create_virtual_account(
        "s001-v1", "S001-v1模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S001", strategy_name_snapshot="综合基线策略",
        strategy_version="v1", release_hash="a" * 64,
        qualification_snapshot="PAPER_READY",
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
        qualification_snapshot="PAPER_READY",
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
        qualification_snapshot="PAPER_READY",
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
