import pytest
from types import SimpleNamespace

from paper_trading_engine.audit import AuditRecorder
from paper_trading_engine.channel_binding import ChannelBindingError, bind_channel_strategy, load_channel_binding
from paper_trading_engine.engine import OrderIntent, PaperTradingSafetyError
from paper_trading_engine.futu_gateway import FutuGateway
from paper_trading_engine.store import PaperStore
from pte_support import FakeBroker, broker_snapshot, make_engine


def test_ft_pte03_channel_binding_and_simulation_safety(tmp_path, monkeypatch):
    store = PaperStore(tmp_path / "binding.db")
    release = {
        "strategy_id": "S001", "version": "v1", "release_id": "S001-v1",
        "release_hash": "a" * 64, "qualification": "PAPER_READY",
    }
    binding = bind_channel_strategy(store, release, "operator", "人工确认", [])
    assert load_channel_binding(store) == binding
    binding_event = store.recent_events()[0]
    assert binding_event["event_type"] == "CHANNEL_STRATEGY_BOUND"
    assert binding_event["category"] == "STRATEGY"
    assert binding_event["strategy_id"] == "S001"
    assert binding_event["actor_type"] == "OPERATOR"
    assert binding_event["actor_id"] == "operator"
    with pytest.raises(ChannelBindingError):
        bind_channel_strategy(store, {**release, "qualification": "RESEARCH"}, "operator", "bad", [])
    store.close()

    engine, engine_store, broker, _ = make_engine(tmp_path / "engine")
    broker.value = broker_snapshot()
    broker.value = type(broker.value)(replace_account := type(broker.value.account)("REAL", "CN", 1, 1, 0), (), (), "OK")
    with pytest.raises(PaperTradingSafetyError, match="SIMULATE"):
        engine.refresh_account()
    broker.value = type(broker.value)(type(replace_account)("SIMULATE", "US", 1, 1, 0), (), (), "OK")
    with pytest.raises(PaperTradingSafetyError, match="CN"):
        engine.refresh_account()
    engine_store.close()

    class TradeContext:
        def __init__(self):
            self.place_calls = []
            self.modify_calls = []
        def get_acc_list(self):
            return 0, [{"acc_id": 77, "trd_env": "SIMULATE", "trd_market": "CN"}]
        def accinfo_query(self, **kwargs):
            return 0, [{"cash": 900_000, "total_assets": 1_000_000, "frozen_cash": 0}]
        def position_list_query(self, **kwargs):
            return 0, [{"code": "SH.588080", "qty": 50_000}]
        def order_list_query(self, **kwargs):
            return 0, []
        def place_order(self, **kwargs):
            self.place_calls.append(kwargs)
            return 0, [{
                "order_id": "124", "code": kwargs["code"], "trd_side": kwargs["trd_side"],
                "qty": kwargs["qty"], "price": kwargs["price"], "order_status": "SUBMITTING",
                "dealt_qty": 0, "dealt_avg_price": 0, "remark": kwargs["remark"],
            }]
        def modify_order(self, **kwargs):
            self.modify_calls.append(kwargs)
            return 0, []
        def close(self): pass

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
    trade = TradeContext()
    audit_store = PaperStore(tmp_path / "futu-audit.db")
    gateway = FutuGateway(
        symbol="588080.SH", sdk=sdk, trade_context=trade, quote_context=QuoteContext(),
        audit=AuditRecorder(audit_store),
    )
    snapshot = gateway.snapshot()
    assert snapshot.account.environment == "SIMULATE"
    assert snapshot.quote_health == "DEGRADED_QUOTE"
    assert audit_store.query_audit_events(event_type="EXTERNAL_CALL_SUCCEEDED") == []
    gateway.place_order(OrderIntent("PTE-1", "DEC-1", "588080.SH", "BUY", 1000, 1.68))
    gateway.cancel_order("124")
    assert trade.place_calls[0]["trd_env"] == "SIMULATE"
    assert trade.place_calls[0]["adjust_limit"] == 0
    calls = audit_store.query_audit_events(event_type="EXTERNAL_CALL_SUCCEEDED")
    assert {event["details"]["operation"] for event in calls} == {"place_order", "cancel_order"}
    assert all(event["details"]["service"] == "futu" for event in calls)
    with pytest.raises(PaperTradingSafetyError, match="whitelist"):
        gateway.place_order(OrderIntent("PTE-2", "DEC-2", "159352.SZ", "BUY", 1000, 1.0))
    with pytest.raises(PaperTradingSafetyError, match="100-share"):
        gateway.place_order(OrderIntent("PTE-3", "DEC-3", "588080.SH", "BUY", 50, 1.0))
    audit_store.close()

    from paper_trading_engine import cli

    injected = {}

    def advice_factory(**kwargs):
        injected["advice"] = kwargs.get("audit")
        return object()

    def gateway_factory(**kwargs):
        injected["gateway"] = kwargs.get("audit")
        return FakeBroker()

    monkeypatch.setattr(cli, "seed_runtime_data", lambda *_: None)
    monkeypatch.setattr(cli, "CliAdviceClient", advice_factory)
    monkeypatch.setattr(cli, "FutuGateway", gateway_factory)
    args = cli.build_parser().parse_args(["once", "--repo-root", str(tmp_path / "runtime")])
    operations = cli.build_engine(args)
    assert injected["advice"].store is operations.store
    assert injected["gateway"].store is operations.store
    operations.close()
