from __future__ import annotations

from types import SimpleNamespace

import pytest


class FakeTradeContext:
    def __init__(self):
        self.place_calls = []
        self.modify_calls = []
        self.account_queries = 0
        self.order_queries = 0

    def get_acc_list(self):
        return 0, [{"acc_id": 77, "trd_env": "SIMULATE", "trd_market": "CN"}]

    def accinfo_query(self, **kwargs):
        self.account_queries += 1
        return 0, [{"cash": 900_000.0, "total_assets": 1_010_000.0, "frozen_cash": 10_000.0}]

    def position_list_query(self, **kwargs):
        return 0, [{"code": "SH.588080", "qty": 50_000}]

    def order_list_query(self, **kwargs):
        self.order_queries += 1
        return 0, [
            {
                "order_id": "123",
                "code": "SH.588080",
                "trd_side": "BUY",
                "qty": 50_000,
                "price": 1.688,
                "order_status": "SUBMITTED",
                "dealt_qty": 10_000,
                "dealt_avg_price": 1.687,
                "remark": "PTE-DEC-ONE",
            }
        ]

    def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        return 0, [
            {
                "order_id": "124",
                "code": kwargs["code"],
                "trd_side": kwargs["trd_side"],
                "qty": kwargs["qty"],
                "price": kwargs["price"],
                "order_status": "SUBMITTING",
                "dealt_qty": 0,
                "dealt_avg_price": 0.0,
                "remark": kwargs["remark"],
            }
        ]

    def modify_order(self, **kwargs):
        self.modify_calls.append(kwargs)
        return 0, []

    def close(self):
        pass


class FakeQuoteContext:
    def get_stock_quote(self, code_list):
        return -1, "no quote entitlement"

    def close(self):
        pass


class FakeSysConfig:
    calls = []

    @classmethod
    def enable_console_log(cls, enabled):
        cls.calls.append(enabled)


SDK = SimpleNamespace(
    RET_OK=0,
    TrdEnv=SimpleNamespace(SIMULATE="SIMULATE"),
    TrdMarket=SimpleNamespace(CN="CN"),
    TrdSide=SimpleNamespace(BUY="BUY", SELL="SELL"),
    OrderType=SimpleNamespace(NORMAL="NORMAL"),
    TimeInForce=SimpleNamespace(DAY="DAY"),
    ModifyOrderOp=SimpleNamespace(CANCEL="CANCEL"),
    SysConfig=FakeSysConfig,
)


def gateway():
    from paper_trading_engine.futu_gateway import FutuGateway

    trade = FakeTradeContext()
    FakeSysConfig.calls.clear()
    return FutuGateway(
        symbol="588080.SH",
        sdk=SDK,
        trade_context=trade,
        quote_context=FakeQuoteContext(),
    ), trade


def test_futu_snapshot_normalizes_account_position_order_and_quote_health() -> None:
    futu, _ = gateway()

    snapshot = futu.snapshot()

    assert snapshot.account.environment == "SIMULATE"
    assert snapshot.account.market == "CN"
    assert snapshot.account.cash == 900_000.0
    assert snapshot.positions[0].symbol == "588080.SH"
    assert snapshot.positions[0].quantity == 50_000
    assert snapshot.orders[0].channel_order_id == "123"
    assert snapshot.orders[0].cumulative_filled_quantity == 10_000
    assert snapshot.quote_health == "DEGRADED_QUOTE"
    assert FakeSysConfig.calls == [False]


def test_futu_place_order_hard_locks_parameters_and_disables_adjustment() -> None:
    from paper_trading_engine.engine import OrderIntent

    futu, trade = gateway()
    intent = OrderIntent("PTE-DEC-ONE", "DEC-ONE", "588080.SH", "BUY", 50_000, 1.688)

    order = futu.place_order(intent)

    assert order.channel_order_id == "124"
    assert trade.place_calls == [
        {
            "price": 1.688,
            "qty": 50_000,
            "code": "SH.588080",
            "trd_side": "BUY",
            "order_type": "NORMAL",
            "adjust_limit": 0,
            "trd_env": "SIMULATE",
            "acc_id": 77,
            "remark": "PTE-DEC-ONE",
            "time_in_force": "DAY",
        }
    ]


def test_futu_rejects_wrong_symbol_or_lot_before_sdk_call() -> None:
    from paper_trading_engine.engine import OrderIntent, PaperTradingSafetyError

    futu, trade = gateway()
    with pytest.raises(PaperTradingSafetyError, match="whitelist"):
        futu.place_order(OrderIntent("x", "d", "159352.SZ", "BUY", 50_000, 1.0))
    with pytest.raises(PaperTradingSafetyError, match="100-share"):
        futu.place_order(OrderIntent("x", "d", "588080.SH", "BUY", 50, 1.0))
    assert trade.place_calls == []


def test_futu_cancel_uses_simulate_account_and_no_price_adjustment() -> None:
    futu, trade = gateway()

    futu.cancel_order("123")

    assert trade.modify_calls == [
        {
            "modify_order_op": "CANCEL",
            "order_id": "123",
            "qty": 0,
            "price": 0,
            "adjust_limit": 0,
            "trd_env": "SIMULATE",
            "acc_id": 77,
        }
    ]


def test_futu_maps_sdk_failure_to_stable_gateway_error() -> None:
    from paper_trading_engine.futu_gateway import FutuGatewayError

    futu, _ = gateway()
    futu.trade_context.get_acc_list = lambda: (-1, "OpenD disconnected")
    with pytest.raises(FutuGatewayError, match="get_acc_list"):
        futu.snapshot()


def test_futu_order_poll_does_not_refresh_account() -> None:
    futu, trade = gateway()

    orders = futu.order_snapshot()

    assert len(orders) == 1
    assert trade.order_queries == 1
    assert trade.account_queries == 0
