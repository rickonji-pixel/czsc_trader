"""Immutable public records for PTE virtual-account ledgers."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class VirtualAccount:
    account_id: str
    name: str
    baseline_version: str
    baseline_sha256: str
    initial_cash: Decimal
    cash: Decimal
    quantity: int
    average_cost: Decimal
    realized_pnl: Decimal
    cycle_target: int | None
    paused: bool


@dataclass(frozen=True)
class VirtualOrder:
    order_id: str
    account_id: str
    decision_id: str
    valid_session: str
    side: str
    quantity: int
    limit_price: Decimal
    status: str


@dataclass(frozen=True)
class VirtualFill:
    fill_id: str
    account_id: str
    order_id: str
    session: str
    side: str
    quantity: int
    price: Decimal
    fee: Decimal
    realized_pnl: Decimal


@dataclass(frozen=True)
class VirtualSnapshot:
    account_id: str
    session: str
    close: Decimal
    total_assets: Decimal
