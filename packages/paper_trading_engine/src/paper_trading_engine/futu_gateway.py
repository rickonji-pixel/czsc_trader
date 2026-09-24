"""Futu OpenAPI adapter locked to an explicit CN or US simulated-stock account."""

from __future__ import annotations

from typing import Any
import time
import re

from .audit import AuditRecorder
from .channel import futu_simulate_channel_id

from .broker import (
    BrokerAccount,
    BrokerOrder,
    BrokerOrderRejectedError,
    BrokerPosition,
    BrokerSnapshot,
    BrokerSubmissionUncertainError,
    OrderIntent,
    PaperTradingSafetyError,
)


class FutuGatewayError(RuntimeError):
    pass


_DEFINITE_REJECTION_MARKERS = (
    "报单价格",
    "涨跌停",
    "购买力不足",
    "持仓不足",
    "交易权限",
    "市场未开放",
    "参数错误",
    "invalid price",
    "insufficient",
    "not tradable",
    "rejected",
)


def _submission_error(operation: str, value: object) -> RuntimeError:
    message = f"{operation} failed: {value}"
    lowered = str(value).lower()
    if any(marker in lowered for marker in _DEFINITE_REJECTION_MARKERS):
        return BrokerOrderRejectedError(message)
    return BrokerSubmissionUncertainError(message)


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
        symbol: str | None = None,
        market: str = "CN",
        account_id: int | None = None,
        host: str = "127.0.0.1",
        port: int = 11111,
        sdk: object | None = None,
        trade_context: object | None = None,
        audit: AuditRecorder | None = None,
    ) -> None:
        if sdk is None:
            import futu as sdk_module

            sdk = sdk_module
        self.sdk = sdk
        self.sdk.SysConfig.enable_console_log(False)
        self.market = str(market).strip().upper()
        self.channel_id = futu_simulate_channel_id(self.market)
        self._sdk_market = getattr(sdk.TrdMarket, self.market)
        self.symbol = symbol.upper() if symbol else None
        self.trade_context = trade_context or sdk.OpenSecTradeContext(
            filter_trdmarket=self._sdk_market, host=host, port=port
        )
        self._configured_account_id = None if account_id is None else int(account_id)
        self._account_id: int | None = None
        self.audit = audit

    def _audit_mutation(
        self, operation: str, started: float, *, correlation_id: str,
        order_id: str | None = None, account_id: str | None = None,
        error: Exception | None = None,
        symbol: str | None = None,
    ) -> None:
        if self.audit is None:
            return
        self.audit.record(
            "EXTERNAL_CALL_FAILED" if error else "EXTERNAL_CALL_SUCCEEDED",
            source="futu_gateway", outcome="FAILURE" if error else "SUCCESS",
            actor_type="EXTERNAL", actor_id="futu", correlation_id=correlation_id,
            account_id=account_id, symbol=symbol or self.symbol, channel=self.channel_id, order_id=order_id,
            details={
                "service": "futu", "operation": operation,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                **({"error_type": type(error).__name__, "error": str(error)} if error else {}),
            },
        )

    def _ok(self, operation: str, result: tuple[object, object]) -> object:
        code, value = result
        if code != self.sdk.RET_OK:
            raise FutuGatewayError(f"{operation} failed: {value}")
        return value

    def _account(self) -> int:
        if self._account_id is not None:
            return self._account_id
        rows = _records(self._ok("get_acc_list", self.trade_context.get_acc_list()))
        # OpenD account-list rows may omit ``trd_market``.  The trade context
        # is constructed with a fixed market; any returned market must agree.
        matches = [
            row for row in rows
            if row.get("trd_env") == self.sdk.TrdEnv.SIMULATE
            and row.get("trd_market", self._sdk_market) == self._sdk_market
            and (
                not row.get("trdmarket_auth")
                or self._sdk_market in row.get("trdmarket_auth", [])
            )
            and str(row.get("sim_acc_type", "")).upper() not in {"OPTION", "FUTURES"}
            and (
                self._configured_account_id is None
                or int(row.get("acc_id", -1)) == self._configured_account_id
            )
        ]
        if len(matches) != 1:
            raise FutuGatewayError(
                f"expected one {self.market} SIMULATE stock account, found {len(matches)}"
            )
        self._account_id = int(matches[0]["acc_id"])
        return self._account_id

    def snapshot(self) -> BrokerSnapshot:
        account = self.account_snapshot()
        return BrokerSnapshot(
            account.account,
            account.positions,
            self.order_snapshot(),
        )

    def account_snapshot(self) -> BrokerSnapshot:
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
                self.trade_context.position_list_query(**common),
            )
        )
        return BrokerSnapshot(
            account=BrokerAccount(
                environment="SIMULATE",
                market=self.market,
                cash=float(account_row.get("cash", 0.0)),
                total_assets=float(account_row.get("total_assets", 0.0)),
                frozen_cash=float(account_row.get("frozen_cash", 0.0)),
                market_value=float(account_row.get("market_val", 0.0)),
            ),
            positions=tuple(
                BrokerPosition(_project_symbol(str(row["code"])), int(row["qty"]))
                for row in position_rows
            ),
            orders=(),
        )

    def order_snapshot(self) -> tuple[BrokerOrder, ...]:
        rows = _records(
            self._ok(
                "order_list_query",
                self.trade_context.order_list_query(
                    trd_env=self.sdk.TrdEnv.SIMULATE,
                    acc_id=self._account(),
                    refresh_cache=True,
                ),
            )
        )
        return tuple(self._map_order(row) for row in rows)

    def historical_order_snapshot(self, start: str, end: str) -> tuple[BrokerOrder, ...]:
        rows = _records(
            self._ok(
                "history_order_list_query",
                self.trade_context.history_order_list_query(
                    start=start,
                    end=end,
                    trd_env=self.sdk.TrdEnv.SIMULATE,
                    acc_id=self._account(),
                ),
            )
        )
        return tuple(self._map_order(row) for row in rows)

    def _map_order(self, row: dict[str, Any]) -> BrokerOrder:
        raw_order_type = str(row.get("order_type", "NORMAL")).upper()
        order_type = "MARKET" if "MARKET" in raw_order_type else "LIMIT"
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
            last_error=str(row.get("last_err_msg", "")),
            created_at=str(row.get("create_time", "")),
            updated_at=str(row.get("updated_time", "")),
            order_type=order_type,
        )

    def place_order(self, intent: OrderIntent) -> BrokerOrder:
        symbol_pattern = r"[0-9]{6}\.(SH|SZ)" if self.market == "CN" else r"[A-Z][A-Z0-9.-]*\.US"
        if re.fullmatch(symbol_pattern, intent.symbol.upper()) is None:
            label = "China-market" if self.market == "CN" else "US-market"
            raise PaperTradingSafetyError(f"order symbol is not a supported {label} code")
        lot_size = 100 if self.market == "CN" else 1
        if intent.quantity <= 0 or intent.quantity % lot_size:
            raise PaperTradingSafetyError(f"order quantity must use positive {lot_size}-share lots")
        if intent.side not in {"BUY", "SELL"}:
            raise PaperTradingSafetyError("order side must be BUY or SELL")
        if intent.time_in_force != "DAY":
            raise PaperTradingSafetyError("gateway accepts DAY orders only")
        supported = {
            ("BUY", "LIMIT"), ("SELL", "LIMIT"), ("SELL", "MARKET"),
        }
        if self.market == "US":
            supported.add(("BUY", "MARKET"))
        if (intent.side, intent.order_type) not in supported:
            raise PaperTradingSafetyError(
                f"gateway does not support {intent.side} {intent.order_type} in {self.market}"
            )
        side = self.sdk.TrdSide.BUY if intent.side == "BUY" else self.sdk.TrdSide.SELL
        order_type = (
            self.sdk.OrderType.NORMAL
            if intent.order_type == "LIMIT"
            else self.sdk.OrderType.MARKET
        )
        started = time.perf_counter()
        try:
            code, value = self.trade_context.place_order(
                price=intent.limit_price,
                qty=intent.quantity,
                code=_broker_code(intent.symbol),
                trd_side=side,
                order_type=order_type,
                adjust_limit=0,
                trd_env=self.sdk.TrdEnv.SIMULATE,
                acc_id=self._account(),
                remark=intent.intent_id,
                time_in_force=self.sdk.TimeInForce.DAY,
            )
            if code != self.sdk.RET_OK:
                raise _submission_error("place_order", value)
            rows = _records(value)
            if len(rows) != 1:
                raise FutuGatewayError("place_order did not return exactly one order")
            order = self._map_order(rows[0])
        except Exception as exc:
            self._audit_mutation(
                "place_order", started, correlation_id=intent.decision_id,
                account_id=intent.account_id, error=exc, symbol=intent.symbol,
            )
            raise
        self._audit_mutation(
            "place_order", started, correlation_id=intent.decision_id,
            order_id=order.channel_order_id, account_id=intent.account_id,
            symbol=intent.symbol,
        )
        return order

    def cancel_order(self, channel_order_id: str) -> None:
        started = time.perf_counter()
        try:
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
        except Exception as exc:
            self._audit_mutation(
                "cancel_order", started, correlation_id=channel_order_id,
                order_id=channel_order_id, error=exc,
            )
            raise
        self._audit_mutation(
            "cancel_order", started, correlation_id=channel_order_id,
            order_id=channel_order_id,
        )

    def close(self) -> None:
        self.trade_context.close()
