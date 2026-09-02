"""Futu OpenAPI adapter hard-locked to China simulated trading."""

from __future__ import annotations

from typing import Any

from .engine import (
    BrokerAccount,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    OrderIntent,
    PaperTradingSafetyError,
)


class FutuGatewayError(RuntimeError):
    pass


def _records(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(item) for item in value]
    if hasattr(value, "to_dict"):
        records = value.to_dict("records")
        return [dict(item) for item in records]
    raise FutuGatewayError(f"unexpected Futu table type: {type(value).__name__}")


def _broker_code(symbol: str) -> str:
    code, market = symbol.upper().split(".", 1)
    return f"{market}.{code}"


def _project_symbol(code: str) -> str:
    market, symbol = code.upper().split(".", 1)
    return f"{symbol}.{market}"


class FutuGateway:
    def __init__(
        self,
        *,
        symbol: str,
        host: str = "127.0.0.1",
        port: int = 11111,
        sdk: object | None = None,
        trade_context: object | None = None,
        quote_context: object | None = None,
    ) -> None:
        if sdk is None:
            import futu as sdk_module

            sdk = sdk_module
        self.sdk = sdk
        self.symbol = symbol.upper()
        self.code = _broker_code(self.symbol)
        self.trade_context = trade_context or sdk.OpenSecTradeContext(
            filter_trdmarket=sdk.TrdMarket.CN, host=host, port=port
        )
        self.quote_context = quote_context or sdk.OpenQuoteContext(host=host, port=port)
        self._account_id: int | None = None

    def _ok(self, operation: str, result: tuple[object, object]) -> object:
        code, value = result
        if code != self.sdk.RET_OK:
            raise FutuGatewayError(f"{operation} failed: {value}")
        return value

    def _account(self) -> int:
        if self._account_id is not None:
            return self._account_id
        rows = _records(self._ok("get_acc_list", self.trade_context.get_acc_list()))
        matches = [
            row
            for row in rows
            if row.get("trd_env") == self.sdk.TrdEnv.SIMULATE
            and row.get("trd_market", self.sdk.TrdMarket.CN) == self.sdk.TrdMarket.CN
        ]
        if len(matches) != 1:
            raise FutuGatewayError(f"expected one CN SIMULATE account, found {len(matches)}")
        self._account_id = int(matches[0]["acc_id"])
        return self._account_id

    def snapshot(self) -> BrokerSnapshot:
        account_id = self._account()
        common = {
            "trd_env": self.sdk.TrdEnv.SIMULATE,
            "acc_id": account_id,
            "refresh_cache": True,
        }
        account_rows = _records(
            self._ok("accinfo_query", self.trade_context.accinfo_query(**common))
        )
        if len(account_rows) != 1:
            raise FutuGatewayError("accinfo_query did not return exactly one account")
        account_row = account_rows[0]
        position_rows = _records(
            self._ok(
                "position_list_query",
                self.trade_context.position_list_query(code=self.code, **common),
            )
        )
        order_rows = _records(
            self._ok(
                "order_list_query",
                self.trade_context.order_list_query(code=self.code, **common),
            )
        )
        quote_result, _ = self.quote_context.get_stock_quote([self.code])
        quote_health = "OK" if quote_result == self.sdk.RET_OK else "DEGRADED_QUOTE"
        return BrokerSnapshot(
            account=BrokerAccount(
                environment="SIMULATE",
                market="CN",
                cash=float(account_row.get("cash", 0.0)),
                total_assets=float(account_row.get("total_assets", 0.0)),
                frozen_cash=float(account_row.get("frozen_cash", 0.0)),
            ),
            positions=tuple(
                BrokerPosition(_project_symbol(str(row["code"])), int(row["qty"]))
                for row in position_rows
            ),
            orders=tuple(self._map_order(row) for row in order_rows),
            quote_health=quote_health,
        )

    def _map_order(self, row: dict[str, Any]) -> BrokerOrder:
        return BrokerOrder(
            channel_order_id=str(row["order_id"]),
            symbol=_project_symbol(str(row["code"])),
            side=str(row["trd_side"]),
            quantity=int(row["qty"]),
            limit_price=float(row["price"]),
            status=str(row["order_status"]),
            cumulative_filled_quantity=int(row.get("dealt_qty", 0)),
            average_fill_price=float(row.get("dealt_avg_price", 0.0)),
            remark=str(row.get("remark", "")),
        )

    def place_order(self, intent: OrderIntent) -> BrokerOrder:
        if intent.symbol != self.symbol:
            raise PaperTradingSafetyError("order symbol differs from gateway whitelist")
        if intent.quantity <= 0 or intent.quantity % 100:
            raise PaperTradingSafetyError("order quantity must use positive 100-share lots")
        if intent.order_type != "LIMIT" or intent.time_in_force != "DAY":
            raise PaperTradingSafetyError("gateway accepts DAY limit orders only")
        side = self.sdk.TrdSide.BUY if intent.side == "BUY" else self.sdk.TrdSide.SELL
        rows = _records(
            self._ok(
                "place_order",
                self.trade_context.place_order(
                    price=intent.limit_price,
                    qty=intent.quantity,
                    code=self.code,
                    trd_side=side,
                    order_type=self.sdk.OrderType.NORMAL,
                    adjust_limit=0,
                    trd_env=self.sdk.TrdEnv.SIMULATE,
                    acc_id=self._account(),
                    remark=intent.intent_id,
                    time_in_force=self.sdk.TimeInForce.DAY,
                ),
            )
        )
        if len(rows) != 1:
            raise FutuGatewayError("place_order did not return exactly one order")
        return self._map_order(rows[0])

    def cancel_order(self, channel_order_id: str) -> None:
        self._ok(
            "modify_order",
            self.trade_context.modify_order(
                modify_order_op=self.sdk.ModifyOrderOp.CANCEL,
                order_id=channel_order_id,
                qty=0,
                price=0,
                adjust_limit=0,
                trd_env=self.sdk.TrdEnv.SIMULATE,
                acc_id=self._account(),
            ),
        )

    def close(self) -> None:
        self.quote_context.close()
        self.trade_context.close()
