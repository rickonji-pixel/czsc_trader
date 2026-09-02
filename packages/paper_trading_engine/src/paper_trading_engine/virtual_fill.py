"""Deterministic, conservative fills for internal virtual accounts."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class FillOutcome:
    status: str
    quantity: int = 0
    price: Decimal | None = None


def settle_limit_order(side: str, quantity: int, limit_price: Decimal, open_price: Decimal, high: Decimal, low: Decimal) -> FillOutcome:
    if quantity <= 0 or quantity % 100:
        raise ValueError("quantity must use positive 100-share lots")
    if min(limit_price, open_price, high, low) <= 0 or high < max(open_price, low) or low > open_price:
        raise ValueError("invalid OHLC or limit price")
    if side == "BUY":
        if open_price <= limit_price:
            return FillOutcome("FILLED", quantity, open_price)
        if low < limit_price:
            return FillOutcome("FILLED", quantity, limit_price)
        if low == limit_price:
            return FillOutcome("TOUCH_UNCERTAIN")
        return FillOutcome("UNFILLED")
    if side == "SELL":
        if open_price >= limit_price:
            return FillOutcome("FILLED", quantity, open_price)
        if high > limit_price:
            return FillOutcome("FILLED", quantity, limit_price)
        if high == limit_price:
            return FillOutcome("TOUCH_UNCERTAIN")
        return FillOutcome("UNFILLED")
    raise ValueError("side must be BUY or SELL")
