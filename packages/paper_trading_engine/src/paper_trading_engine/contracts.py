"""Stable values crossing the strategy and runtime process boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
import math
import re
from typing import Any


class AdviceContractError(ValueError):
    """The strategy CLI emitted a payload PTE cannot execute safely."""


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AdviceContractError(f"{name} must be an object")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AdviceContractError(f"{name} must be an integer")
    return value


def _finite_number(value: object, name: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AdviceContractError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or (number < 0 if nonnegative else number <= 0):
        qualifier = "non-negative" if nonnegative else "positive"
        raise AdviceContractError(f"{name} must be {qualifier} and finite")
    return number


@dataclass(frozen=True)
class OrderSpec:
    side: str
    quantity: int
    order_type: str
    limit_price: float
    time_in_force: str

    @classmethod
    def from_payload(cls, payload: object) -> "OrderSpec":
        value = _object(payload, "order")
        price = value.get("limit_price")
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            raise AdviceContractError("order limit price must be numeric")
        price = float(price)
        if not math.isfinite(price) or price <= 0:
            raise AdviceContractError("order limit price must be positive and finite")
        quantity = _integer(value.get("quantity"), "order quantity")
        if quantity <= 0 or quantity % 100:
            raise AdviceContractError("order quantity must use positive 100-share lots")
        side = str(value.get("side", ""))
        if side not in {"BUY", "SELL"}:
            raise AdviceContractError("order side must be BUY or SELL")
        order_type = str(value.get("order_type", ""))
        if order_type not in {"LIMIT", "MARKET"}:
            raise AdviceContractError("order type must be LIMIT or MARKET")
        if side == "BUY" and order_type != "LIMIT":
            raise AdviceContractError("buy orders must use LIMIT")
        if side == "SELL" and order_type != "MARKET":
            raise AdviceContractError("sell orders must use MARKET")
        if value.get("time_in_force") != "DAY":
            raise AdviceContractError("order time in force must be DAY")
        return cls(side, quantity, order_type, price, "DAY")


@dataclass(frozen=True)
class PlanLegSpec:
    sequence: int
    role: str
    checkpoint: str
    submit_after: time
    submit_before: time
    dependency_sequence: int | None
    dependency_required_status: str | None
    order: OrderSpec

    @classmethod
    def from_payload(cls, payload: object) -> "PlanLegSpec":
        value = _object(payload, "execution plan leg")
        sequence = _integer(value.get("sequence"), "execution plan leg sequence")
        if sequence < 0:
            raise AdviceContractError("execution plan leg sequence must be non-negative")
        role = str(value.get("role", ""))
        checkpoint = str(value.get("checkpoint", ""))
        allowed = {
            "CORE_SETUP": "OPEN",
            "ROTATION_ENTRY": "OPEN",
            "ROTATION_EXIT": "11:30_CLOSE",
        }
        if role not in allowed or checkpoint != allowed[role]:
            raise AdviceContractError("execution plan leg role or checkpoint is invalid")
        try:
            submit_after = time.fromisoformat(str(value["submit_after"]))
            submit_before = time.fromisoformat(str(value["submit_before"]))
        except (KeyError, ValueError) as exc:
            raise AdviceContractError("execution plan times must use ISO format") from exc
        if submit_after >= submit_before:
            raise AdviceContractError("execution plan submission window is empty")
        dependency = value.get("dependency_sequence")
        if dependency is not None:
            dependency = _integer(dependency, "execution plan dependency sequence")
            if dependency < 0 or dependency >= sequence:
                raise AdviceContractError("execution plan dependency must reference an earlier leg")
        required_status = value.get("dependency_required_status")
        required_status = None if required_status is None else str(required_status)
        if (dependency is None) != (required_status is None):
            raise AdviceContractError("execution plan dependency status is incomplete")
        if required_status not in {None, "FILLED_ALL"}:
            raise AdviceContractError("execution plan dependency must require FILLED_ALL")
        order = OrderSpec.from_payload(value.get("order"))
        if role in {"CORE_SETUP", "ROTATION_ENTRY"} and order.side != "BUY":
            raise AdviceContractError("entry execution plan legs must buy")
        if role == "ROTATION_EXIT" and order.side != "SELL":
            raise AdviceContractError("exit execution plan leg must sell")
        return cls(
            sequence, role, checkpoint, submit_after, submit_before,
            dependency, required_status, order,
        )


@dataclass(frozen=True)
class AdviceDecision:
    contract_version: str
    decision_id: str
    symbol: str
    signal_date: date
    valid_session: date
    actual_quantity: int
    target_quantity: int
    cycle_target_quantity: int
    delta_quantity: int
    action: str
    strategy: dict[str, str]
    signal_reference_price: float
    execution_reference_price: float
    data_cutoff: date
    order: OrderSpec | None
    orders: tuple[OrderSpec, ...] = ()
    available_cash: float = 0.0
    fee_rate: float = 0.0
    estimated_order_cost: float = 0.0
    unallocated_cash: float = 0.0
    source_decision_id: str = ""
    plan_mode: str = "NONE"
    plan_legs: tuple[PlanLegSpec, ...] = ()

    @classmethod
    def from_cli_payload(cls, payload: object) -> "AdviceDecision":
        outer = _object(payload, "CLI payload")
        if outer.get("status") != "PASS":
            raise AdviceContractError("advice command failed")
        value = _object(outer.get("result"), "result")
        version = str(value.get("contract_version", ""))
        if version not in {"advice.v4", "advice.v5"}:
            raise AdviceContractError(f"unsupported advice contract version: {version}")
        try:
            signal_date = date.fromisoformat(str(value["signal_date"]))
            valid_session = date.fromisoformat(str(value["valid_session"]))
            data_cutoff = date.fromisoformat(str(value["data_cutoff"]))
        except (KeyError, ValueError) as exc:
            raise AdviceContractError("advice dates must use ISO format") from exc
        actual = _integer(value.get("actual_quantity"), "actual quantity")
        target = _integer(value.get("target_quantity"), "target quantity")
        cycle_target = _integer(value.get("cycle_target_quantity"), "cycle target quantity")
        delta = _integer(value.get("delta_quantity"), "delta quantity")
        if actual < 0 or target < 0 or cycle_target < 0 or any(q % 100 for q in (actual, target, cycle_target)):
            raise AdviceContractError("advice quantities must use non-negative 100-share lots")
        if target - actual != delta:
            raise AdviceContractError("delta quantity does not match target minus actual")
        order_payload = value.get("order")
        order = None if order_payload is None else OrderSpec.from_payload(order_payload)
        orders_value = value.get("orders")
        if not isinstance(orders_value, list):
            raise AdviceContractError("orders must be a list")
        orders = tuple(OrderSpec.from_payload(item) for item in orders_value)
        action = str(value.get("action", ""))
        plan_mode = str(value.get("plan_mode", "NONE"))
        plan_value = value.get("plan_legs", [])
        if not isinstance(plan_value, list):
            raise AdviceContractError("plan_legs must be a list")
        plan_legs = tuple(PlanLegSpec.from_payload(item) for item in plan_value)
        if tuple(item.sequence for item in plan_legs) != tuple(range(len(plan_legs))):
            raise AdviceContractError("execution plan leg sequences must be contiguous")
        if version == "advice.v4":
            if plan_mode != "NONE" or plan_legs:
                raise AdviceContractError("advice.v4 cannot contain an execution plan")
            if (delta == 0) != (len(orders) == 0):
                raise AdviceContractError("orders presence does not match quantity delta")
            if orders:
                expected_side = "BUY" if delta > 0 else "SELL"
                if any(item.side != expected_side for item in orders) or sum(item.quantity for item in orders) != abs(delta) or action != expected_side:
                    raise AdviceContractError("order does not match action and quantity delta")
            if (len(orders) == 1 and order != orders[0]) or (len(orders) != 1 and order is not None):
                raise AdviceContractError("order shortcut does not match orders")
        else:
            if orders or order is not None:
                raise AdviceContractError("advice.v5 planned decisions cannot contain immediate orders")
            if plan_mode == "NONE":
                if plan_legs or action not in {"HOLD", "WAIT"} or delta != 0:
                    raise AdviceContractError("empty execution plan must be a zero-delta hold")
            elif plan_mode == "CORE_SETUP":
                if (
                    action != "BUY" or delta <= 0 or len(plan_legs) != 1
                    or plan_legs[0].role != "CORE_SETUP"
                    or plan_legs[0].order.quantity != delta
                    or plan_legs[0].dependency_sequence is not None
                ):
                    raise AdviceContractError("core setup execution plan is inconsistent")
            elif plan_mode == "CORE_EVENT_INTRADAY_ROTATION":
                if (
                    action != "ROTATE" or delta != 0 or len(plan_legs) != 2
                    or tuple(item.role for item in plan_legs)
                    != ("ROTATION_ENTRY", "ROTATION_EXIT")
                    or plan_legs[1].dependency_sequence != 0
                    or plan_legs[1].dependency_required_status != "FILLED_ALL"
                    or plan_legs[0].order.quantity != plan_legs[1].order.quantity
                ):
                    raise AdviceContractError("intraday rotation execution plan is inconsistent")
            else:
                raise AdviceContractError("unsupported execution plan mode")
        strategy = _object(value.get("strategy"), "strategy")
        expected_strategy_fields = {
            "strategy_id",
            "name",
            "version",
            "release_id",
            "release_hash",
            "qualification",
        }
        if set(strategy) != expected_strategy_fields:
            raise AdviceContractError("strategy identity fields are incomplete")
        strategy_id = str(strategy["strategy_id"])
        strategy_version = str(strategy["version"])
        release_hash = str(strategy["release_hash"])
        if re.fullmatch(r"S[0-9]{3}", strategy_id) is None:
            raise AdviceContractError("strategy id has invalid format")
        if re.fullmatch(r"v[1-9][0-9]*", strategy_version) is None:
            raise AdviceContractError("strategy version has invalid format")
        if strategy["release_id"] != f"{strategy_id}-{strategy_version}":
            raise AdviceContractError("strategy release id is inconsistent")
        if re.fullmatch(r"[0-9a-f]{64}", release_hash) is None:
            raise AdviceContractError("strategy release hash has invalid format")
        if strategy["qualification"] not in {"PAPER_READY", "LIVE_READY"}:
            raise AdviceContractError("strategy qualification does not permit paper trading")
        if not str(strategy["name"]).strip():
            raise AdviceContractError("strategy name is required")
        signal_reference = _finite_number(
            value.get("signal_reference_price"), "signal reference price"
        )
        execution_reference = _finite_number(
            value.get("execution_reference_price"), "execution reference price"
        )
        available_cash = _finite_number(
            value.get("available_cash"), "available cash", nonnegative=True
        )
        fee_rate = _finite_number(value.get("fee_rate"), "fee rate", nonnegative=True)
        estimated_cost = _finite_number(
            value.get("estimated_order_cost", 0.0), "estimated order cost", nonnegative=True
        )
        unallocated_cash = _finite_number(
            value.get("unallocated_cash", available_cash),
            "unallocated cash", nonnegative=True,
        )
        if valid_session <= signal_date:
            raise AdviceContractError("valid session must be after signal date")
        if data_cutoff != signal_date:
            raise AdviceContractError("data cutoff must equal signal date")
        if not str(value.get("decision_id", "")).strip():
            raise AdviceContractError("decision id is required")
        if re.fullmatch(r"[0-9]{6}\.(SH|SZ)", str(value.get("symbol", "")).upper()) is None:
            raise AdviceContractError("advice symbol has invalid format")
        return cls(
            contract_version=version,
            decision_id=str(value.get("decision_id", "")),
            symbol=str(value.get("symbol", "")).upper(),
            signal_date=signal_date,
            valid_session=valid_session,
            actual_quantity=actual,
            target_quantity=target,
            cycle_target_quantity=cycle_target,
            delta_quantity=delta,
            action=action,
            strategy={key: str(strategy[key]) for key in expected_strategy_fields},
            signal_reference_price=signal_reference,
            execution_reference_price=execution_reference,
            data_cutoff=data_cutoff,
            order=order,
            orders=orders,
            available_cash=available_cash,
            fee_rate=fee_rate,
            estimated_order_cost=estimated_cost,
            unallocated_cash=unallocated_cash,
            source_decision_id=str(value.get("decision_id", "")),
            plan_mode=plan_mode,
            plan_legs=plan_legs,
        )
