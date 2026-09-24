"""Narrow Longbridge order-write adapter for reviewed live execution.

The adapter validates one US-stock order at a time.  It does not decide whether an
order is authorized; :mod:`live_execution_gateway.live_engine` owns that gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from dataflows.config import get_longbridge_credentials

from .longbridge_readonly import require_us_stock


class LongbridgeSubmissionUncertain(RuntimeError):
    """The SDK call failed after submission may have reached Longbridge."""


@dataclass(frozen=True, slots=True)
class LiveOrderRequest:
    intent_id: str
    client_request_id: str
    symbol: str
    side: str
    quantity: int
    order_type: str
    time_in_force: str
    reference_price: Decimal
    outside_rth: str = "RTH_ONLY"

    def __post_init__(self) -> None:
        require_us_stock(self.symbol)
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("live order side must be BUY or SELL")
        if self.order_type not in {"LIMIT", "MARKET"}:
            raise ValueError("live order type must be LIMIT or MARKET")
        if self.time_in_force != "DAY":
            raise ValueError("live orders must use DAY time in force")
        if self.outside_rth not in {"RTH_ONLY", "ANY_TIME", "OVERNIGHT"}:
            raise ValueError("unsupported Longbridge US-stock trading session")
        if self.outside_rth != "RTH_ONLY" and self.order_type != "LIMIT":
            raise ValueError("US pre/post/overnight orders must be LIMIT orders")
        if isinstance(self.quantity, bool) or self.quantity <= 0:
            raise ValueError("live US-stock quantity must be a positive whole share count")
        if Decimal(self.reference_price) <= 0:
            raise ValueError("live order reference price must be positive")
        if not self.intent_id.startswith("LBI-") or len(self.intent_id) > 64:
            raise ValueError("live intent id is invalid for Longbridge remark")
        if not self.client_request_id or len(self.client_request_id) > 64:
            raise ValueError("Longbridge client_request_id must be 1-64 characters")


class LongbridgeTradingGateway:
    """The only module allowed to call Longbridge trade mutations."""

    def __init__(
        self,
        *,
        env_file: str | Path | None = None,
        trade_context: object | None = None,
        sdk: object | None = None,
    ) -> None:
        if sdk is None:
            import longbridge.openapi as sdk_module

            sdk = sdk_module
        if trade_context is None:
            credentials = get_longbridge_credentials(env_file)
            config = sdk.Config.from_apikey(
                credentials.app_key,
                credentials.app_secret,
                credentials.access_token,
                enable_overnight=False,
                enable_print_quote_packages=False,
            )
            trade_context = sdk.TradeContext(config)
        self.__context = trade_context
        self.__sdk = sdk

    def submit(self, request: LiveOrderRequest) -> str:
        side = self.__sdk.OrderSide.Buy if request.side == "BUY" else self.__sdk.OrderSide.Sell
        order_type = (
            self.__sdk.OrderType.LO
            if request.order_type == "LIMIT"
            else self.__sdk.OrderType.MO
        )
        kwargs: dict[str, Any] = {
            "symbol": request.symbol,
            "order_type": order_type,
            "side": side,
            "submitted_quantity": Decimal(request.quantity),
            "time_in_force": self.__sdk.TimeInForceType.Day,
            "outside_rth": {
                "RTH_ONLY": self.__sdk.OutsideRTH.RTHOnly,
                "ANY_TIME": self.__sdk.OutsideRTH.AnyTime,
                "OVERNIGHT": self.__sdk.OutsideRTH.Overnight,
            }[request.outside_rth],
            "remark": request.intent_id,
            "client_request_id": request.client_request_id,
        }
        if request.order_type == "LIMIT":
            kwargs["submitted_price"] = Decimal(request.reference_price)
        try:
            response = self.__context.submit_order(**kwargs)
        except Exception as exc:
            # The SDK does not provide a portable proof that transport exceptions happened
            # before the broker received the request. Never retry automatically.
            raise LongbridgeSubmissionUncertain(str(exc)) from exc
        order_id = str(getattr(response, "order_id", ""))
        if not order_id:
            raise LongbridgeSubmissionUncertain("Longbridge returned no order id")
        return order_id

    def cancel(self, order_id: str) -> None:
        if not str(order_id).strip():
            raise ValueError("Longbridge order id is required")
        try:
            self.__context.cancel_order(str(order_id))
        except Exception as exc:
            raise LongbridgeSubmissionUncertain(f"cancel result is uncertain: {exc}") from exc
