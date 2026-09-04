"""Broker-neutral values used by the Futu execution adapter."""

from dataclasses import dataclass


TERMINAL_ORDER_STATUSES = {
    "SUBMIT_FAILED", "FILLED_ALL", "CANCELLED_ALL", "FAILED", "DISABLED",
    "DELETED", "FILL_CANCELLED",
}


class PaperTradingSafetyError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrokerAccount:
    environment: str
    market: str
    cash: float
    total_assets: float
    frozen_cash: float


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    quantity: int


@dataclass(frozen=True)
class BrokerOrder:
    channel_order_id: str
    symbol: str
    side: str
    quantity: int
    limit_price: float
    status: str
    cumulative_filled_quantity: int
    average_fill_price: float
    remark: str


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    decision_id: str
    symbol: str
    side: str
    quantity: int
    limit_price: float
    order_type: str = "LIMIT"
    time_in_force: str = "DAY"
    account_id: str | None = None


@dataclass(frozen=True)
class BrokerSnapshot:
    account: BrokerAccount
    positions: tuple[BrokerPosition, ...]
    orders: tuple[BrokerOrder, ...]
