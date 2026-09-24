from decimal import Decimal
from types import SimpleNamespace

import pytest

from live_execution_gateway.longbridge_trading import (
    LiveOrderRequest,
    LongbridgeSubmissionUncertain,
    LongbridgeTradingGateway,
)


class FakeSDK:
    class OrderSide:
        Buy = "BUY_ENUM"
        Sell = "SELL_ENUM"

    class OrderType:
        LO = "LIMIT_ENUM"
        MO = "MARKET_ENUM"

    class TimeInForceType:
        Day = "DAY_ENUM"

    class OutsideRTH:
        RTHOnly = "RTH_ONLY_ENUM"
        AnyTime = "ANY_TIME_ENUM"
        Overnight = "OVERNIGHT_ENUM"


class FakeContext:
    def __init__(self, *, error=None):
        self.error = error
        self.submissions = []
        self.cancellations = []

    def submit_order(self, **kwargs):
        self.submissions.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(order_id="LB-1")

    def cancel_order(self, order_id):
        self.cancellations.append(order_id)

def _request(order_type="MARKET", outside_rth="RTH_ONLY") -> LiveOrderRequest:
    return LiveOrderRequest(
        intent_id="LBI-1234567890ABCDEF",
        client_request_id="CZSC-1234567890",
        symbol="MU.US", side="BUY", quantity=2, order_type=order_type,
        time_in_force="DAY", reference_price=Decimal("100.25"),
        outside_rth=outside_rth,
    )


def test_submit_maps_only_reviewed_us_stock_order_fields() -> None:
    context = FakeContext()
    gateway = LongbridgeTradingGateway(trade_context=context, sdk=FakeSDK)

    assert gateway.submit(_request()) == "LB-1"

    payload = context.submissions[0]
    assert payload == {
        "symbol": "MU.US",
        "order_type": "MARKET_ENUM",
        "side": "BUY_ENUM",
        "submitted_quantity": Decimal("2"),
        "time_in_force": "DAY_ENUM",
        "outside_rth": "RTH_ONLY_ENUM",
        "remark": "LBI-1234567890ABCDEF",
        "client_request_id": "CZSC-1234567890",
    }


def test_limit_order_and_cancel_are_explicit() -> None:
    context = FakeContext()
    gateway = LongbridgeTradingGateway(trade_context=context, sdk=FakeSDK)
    gateway.submit(_request("LIMIT"))
    gateway.cancel("LB-1")

    assert context.submissions[0]["submitted_price"] == Decimal("100.25")
    assert context.cancellations == ["LB-1"]


@pytest.mark.parametrize("session,expected", [
    ("ANY_TIME", "ANY_TIME_ENUM"), ("OVERNIGHT", "OVERNIGHT_ENUM"),
])
def test_extended_hours_limit_order_maps_explicit_session(session, expected) -> None:
    context = FakeContext()
    gateway = LongbridgeTradingGateway(trade_context=context, sdk=FakeSDK)
    gateway.submit(_request("LIMIT", session))

    assert context.submissions[0]["outside_rth"] == expected
    assert context.submissions[0]["submitted_price"] == Decimal("100.25")


def test_market_order_cannot_be_routed_to_extended_hours() -> None:
    with pytest.raises(ValueError, match="must be LIMIT"):
        _request("MARKET", "OVERNIGHT")


def test_any_submit_exception_is_uncertain_and_never_classified_for_retry() -> None:
    gateway = LongbridgeTradingGateway(
        trade_context=FakeContext(error=TimeoutError("network")), sdk=FakeSDK,
    )
    with pytest.raises(LongbridgeSubmissionUncertain, match="network"):
        gateway.submit(_request())
