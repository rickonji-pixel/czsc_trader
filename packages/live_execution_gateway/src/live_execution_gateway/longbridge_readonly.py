"""Read-only Longbridge adapter.

This module deliberately exposes no submit, replace, or cancel operation. Order writes
live in a separate adapter so observation code cannot accidentally mutate the account.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import re
from typing import Any

from dataflows.config import get_longbridge_credentials


LONG_BRIDGE_LIVE_US_CHANNEL_ID = "longbridge_live_us"
_US_STOCK = re.compile(r"[A-Z][A-Z0-9.-]*\.US")


class LongbridgeReadOnlyError(RuntimeError):
    """The live account could not be observed without mutating it."""


def require_us_stock(symbol: object) -> str:
    normalized = str(symbol).strip().upper()
    if _US_STOCK.fullmatch(normalized) is None:
        raise ValueError("Longbridge live observation accepts US stocks only")
    return normalized


def _scalar(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, type):
        return value.__name__
    rendered = str(value)
    # longbridge.openapi 5.x exposes enum values as instances whose string
    # representation is e.g. "OrderStatus.New", not the bare "New" used by
    # the REST API and our reconciliation contract.
    enum_type = type(value).__name__
    if enum_type in {
        "OrderStatus", "OrderSide", "OrderType", "TimeInForceType", "OutsideRTH",
    } and rendered.startswith(f"{enum_type}."):
        return rendered.split(".", 1)[1]
    return rendered


def _cash_info(value: object) -> dict[str, object]:
    return {
        key: _scalar(getattr(value, key, None))
        for key in (
            "currency", "available_cash", "withdraw_cash", "frozen_cash", "settling_cash",
        )
    }


def _balance(value: object) -> dict[str, object]:
    return {
        **{
            key: _scalar(getattr(value, key, None))
            for key in (
                "currency", "total_cash", "net_assets", "buy_power", "risk_level",
                "margin_call", "init_margin", "maintenance_margin",
                "max_finance_amount", "remaining_finance_amount",
            )
        },
        "cash_infos": [_cash_info(item) for item in getattr(value, "cash_infos", ())],
    }


def _position(value: object) -> dict[str, object]:
    return {
        key: _scalar(getattr(value, key, None))
        for key in (
            "symbol", "symbol_name", "quantity", "available_quantity", "currency",
            "cost_price", "market", "init_quantity",
        )
    }


def _order(value: object) -> dict[str, object]:
    return {
        key: _scalar(getattr(value, key, None))
        for key in (
            "order_id", "status", "stock_name", "quantity", "executed_quantity", "price",
            "executed_price", "submitted_at", "side", "symbol", "order_type", "last_done",
            "msg", "tag", "time_in_force", "expire_date", "updated_at", "currency",
            "outside_rth", "remark",
        )
    }


def _execution(value: object) -> dict[str, object]:
    return {
        key: _scalar(getattr(value, key, None))
        for key in (
            "order_id", "trade_id", "symbol", "trade_done_at", "quantity", "price", "side",
        )
    }


class LongbridgeReadOnlyGateway:
    """A capability-limited view of one Longbridge US-stock account."""

    channel_id = LONG_BRIDGE_LIVE_US_CHANNEL_ID
    mode = "READ_ONLY"
    order_write_capability = False

    def __init__(
        self,
        *,
        symbol: str,
        env_file: str | Path | None = None,
        trade_context: object | None = None,
    ) -> None:
        self.symbol = require_us_stock(symbol)
        if trade_context is None:
            from longbridge.openapi import Config, TradeContext

            credentials = get_longbridge_credentials(env_file)
            config = Config.from_apikey(
                credentials.app_key,
                credentials.app_secret,
                credentials.access_token,
                enable_overnight=False,
                enable_print_quote_packages=False,
            )
            trade_context = TradeContext(config)
        self.__trade_context = trade_context

    def snapshot(self) -> dict[str, object]:
        """Read current account facts; no order mutation is reachable here."""
        try:
            balances = self.__trade_context.account_balance(currency="USD")
            positions_response = self.__trade_context.stock_positions([self.symbol])
            orders = self.__trade_context.today_orders(symbol=self.symbol)
            executions = self.__trade_context.today_executions(symbol=self.symbol)
        except Exception as exc:
            raise LongbridgeReadOnlyError(f"Longbridge read-only snapshot failed: {exc}") from exc
        positions = [
            _position(position)
            for channel in getattr(positions_response, "channels", ())
            for position in getattr(channel, "positions", ())
        ]
        return {
            "schema": "longbridge_live_snapshot.v1",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "channel_id": self.channel_id,
            "mode": self.mode,
            "order_write_capability": False,
            "symbol": self.symbol,
            "balances": [_balance(item) for item in balances],
            "positions": positions,
            "today_orders": [_order(item) for item in orders],
            "today_executions": [_execution(item) for item in executions],
        }

    def history(self, *, days: int = 30) -> dict[str, object]:
        """Read a bounded recovery window for restart reconciliation."""
        if not 1 <= int(days) <= 90:
            raise ValueError("Longbridge recovery history must be between 1 and 90 days")
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=int(days))
        try:
            orders = self.__trade_context.history_orders(
                symbol=self.symbol, start_at=start, end_at=end,
            )
            executions = self.__trade_context.history_executions(
                symbol=self.symbol, start_at=start, end_at=end,
            )
        except Exception as exc:
            raise LongbridgeReadOnlyError(f"Longbridge read-only history failed: {exc}") from exc
        return {
            "schema": "longbridge_live_history.v1",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "channel_id": self.channel_id,
            "mode": self.mode,
            "order_write_capability": False,
            "symbol": self.symbol,
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "orders": [_order(item) for item in orders],
            "executions": [_execution(item) for item in executions],
        }

    def capability_report(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "mode": self.mode,
            "asset_type": "stock",
            "market": "US",
            "symbol_allowlist": [self.symbol],
            "order_write_capability": False,
            "submit_order_exposed": hasattr(self, "submit_order"),
            "replace_order_exposed": hasattr(self, "replace_order"),
            "cancel_order_exposed": hasattr(self, "cancel_order"),
        }
