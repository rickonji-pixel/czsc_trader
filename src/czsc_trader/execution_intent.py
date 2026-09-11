"""Pure order-intent calculations shared by advice and deterministic replay."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR

from .baselines import ExecutionSpec
from .execution_policy import ceil_to_tick, floor_to_tick, round_to_tick


@dataclass(frozen=True)
class OrderSlice:
    side: str
    quantity: int
    limit_price: float
    order_type: str = "LIMIT"


@dataclass(frozen=True)
class OrderIntent:
    action: str
    target_position: int
    target_quantity: int
    cycle_target_quantity: int | None
    delta_quantity: int
    limit_price: float | None
    orders: tuple[OrderSlice, ...]


def calculate_entry_limit(execution_close: float, execution_spec: ExecutionSpec) -> float:
    """Calculate an entry cap one tick inside the internal exchange boundary."""
    requested = floor_to_tick(
        float(execution_close) * (1.0 + execution_spec.entry_limit_parameter),
        execution_spec.instrument.price_tick,
    )
    tick = execution_spec.instrument.price_tick
    upper_guard = round_to_tick(
        floor_to_tick(
            float(execution_close) * (1.0 + execution_spec.instrument.price_limit_ratio),
            tick,
        ) - tick,
        tick,
    )
    return min(requested, upper_guard)


def calculate_exit_limit(execution_close: float, execution_spec: ExecutionSpec) -> float:
    """Calculate an exit floor one tick inside the internal exchange boundary."""
    tick = execution_spec.instrument.price_tick
    requested = round_to_tick(
        float(execution_close) * (1.0 - execution_spec.exit_limit_ratio),
        tick,
    )
    lower_guard = round_to_tick(
        ceil_to_tick(
            float(execution_close) * (1.0 - execution_spec.instrument.price_limit_ratio),
            tick,
        ) + tick,
        tick,
    )
    return max(requested, lower_guard)


def calculate_target_quantity(
    available_cash: float,
    limit_price: float,
    fee_rate: float,
    lot_size: int,
) -> int:
    """Allocate all affordable cash while respecting fees and board lots."""
    cash = Decimal(str(available_cash))
    unit_cost = Decimal(str(limit_price)) * (Decimal("1") + Decimal(str(fee_rate)))
    lots = (cash / (unit_cost * int(lot_size))).to_integral_value(rounding=ROUND_FLOOR)
    return max(0, int(lots) * int(lot_size))


def _slices(
    side: str, quantity: int, price: float, maximum: int, order_type: str = "LIMIT",
) -> tuple[OrderSlice, ...]:
    result: list[OrderSlice] = []
    remaining = int(quantity)
    while remaining:
        selected = min(remaining, int(maximum))
        result.append(OrderSlice(side, selected, price, order_type))
        remaining -= selected
    return tuple(result)


def decide_order_intent(
    *,
    target_position: int,
    actual_quantity: int,
    cycle_target_quantity: int | None,
    available_cash: float,
    execution_close: float,
    execution_spec: ExecutionSpec | None,
) -> OrderIntent:
    """Map strategy target plus confirmed holdings to one deterministic intent."""
    if execution_spec is None:
        raise ValueError("strategy snapshot requires a complete execution specification")
    instrument = execution_spec.instrument
    if target_position not in (0, 1):
        raise ValueError("target position must be 0 or 1")
    if actual_quantity < 0 or actual_quantity % instrument.lot_size:
        raise ValueError("actual quantity must use complete board lots")
    if cycle_target_quantity is not None and (
        cycle_target_quantity < 0 or cycle_target_quantity % instrument.lot_size
    ):
        raise ValueError("cycle target quantity must use complete board lots")
    if target_position:
        price = calculate_entry_limit(execution_close, execution_spec)
        affordable_target = actual_quantity + calculate_target_quantity(
            available_cash,
            price,
            execution_spec.capital.fee_rate,
            instrument.lot_size,
        )
        cycle_target = (
            affordable_target
            if cycle_target_quantity is None
            else cycle_target_quantity
        )
        target = min(cycle_target, affordable_target)
        delta = max(0, target - actual_quantity)
        action = "BUY" if delta else ("HOLD" if actual_quantity else "WAIT")
        side = "BUY"
    else:
        # A target-zero decision means exit promptly. PTE executes simulated
        # sells as DAY market orders; the reference price remains evidence only.
        price = round_to_tick(float(execution_close), instrument.price_tick)
        target = 0
        delta = -actual_quantity
        action = "SELL" if actual_quantity else "WAIT"
        cycle_target = 0 if not actual_quantity else cycle_target_quantity
        side = "SELL"
    order_type = "LIMIT" if side == "BUY" else "MARKET"
    return OrderIntent(
        action=action,
        target_position=target_position,
        target_quantity=target,
        cycle_target_quantity=cycle_target,
        delta_quantity=delta,
        limit_price=price,
        orders=_slices(
            side, abs(delta), price, instrument.maximum_order_quantity, order_type,
        ),
    )
