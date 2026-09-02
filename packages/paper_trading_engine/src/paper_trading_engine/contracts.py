"""Stable values crossing the strategy and runtime process boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
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
        if value.get("order_type") != "LIMIT":
            raise AdviceContractError("order type must be LIMIT")
        if value.get("time_in_force") != "DAY":
            raise AdviceContractError("order time in force must be DAY")
        return cls(side, quantity, "LIMIT", price, "DAY")


@dataclass(frozen=True)
class AdviceDecision:
    contract_version: str
    decision_id: str
    symbol: str
    signal_date: date
    valid_session: date
    actual_quantity: int
    target_quantity: int
    position_size: int
    delta_quantity: int
    action: str
    baseline: dict[str, str]
    execution_policy: dict[str, str]
    signal_reference_price: float
    execution_reference_price: float
    data_cutoff: date
    order: OrderSpec | None

    @classmethod
    def from_cli_payload(cls, payload: object) -> "AdviceDecision":
        outer = _object(payload, "CLI payload")
        if outer.get("status") != "PASS":
            raise AdviceContractError("advice command failed")
        value = _object(outer.get("result"), "result")
        version = str(value.get("contract_version", ""))
        if version != "advice.v1":
            raise AdviceContractError(f"unsupported advice contract version: {version}")
        try:
            signal_date = date.fromisoformat(str(value["signal_date"]))
            valid_session = date.fromisoformat(str(value["valid_session"]))
            data_cutoff = date.fromisoformat(str(value["data_cutoff"]))
        except (KeyError, ValueError) as exc:
            raise AdviceContractError("advice dates must use ISO format") from exc
        actual = _integer(value.get("actual_quantity"), "actual quantity")
        target = _integer(value.get("target_quantity"), "target quantity")
        size = _integer(value.get("position_size"), "position size")
        delta = _integer(value.get("delta_quantity"), "delta quantity")
        if actual < 0 or target < 0 or size <= 0 or any(q % 100 for q in (actual, target, size)):
            raise AdviceContractError("advice quantities must use non-negative 100-share lots")
        if target - actual != delta:
            raise AdviceContractError("delta quantity does not match target minus actual")
        order_payload = value.get("order")
        order = None if order_payload is None else OrderSpec.from_payload(order_payload)
        action = str(value.get("action", ""))
        if (delta == 0) != (order is None):
            raise AdviceContractError("order presence does not match quantity delta")
        if order is not None:
            expected_side = "BUY" if delta > 0 else "SELL"
            if order.side != expected_side or order.quantity != abs(delta) or action != expected_side:
                raise AdviceContractError("order does not match action and quantity delta")
        baseline = _object(value.get("baseline"), "baseline")
        policy = _object(value.get("execution_policy"), "execution policy")
        return cls(
            contract_version=version,
            decision_id=str(value.get("decision_id", "")),
            symbol=str(value.get("symbol", "")).upper(),
            signal_date=signal_date,
            valid_session=valid_session,
            actual_quantity=actual,
            target_quantity=target,
            position_size=size,
            delta_quantity=delta,
            action=action,
            baseline={"version": str(baseline.get("version", "")), "sha256": str(baseline.get("sha256", ""))},
            execution_policy={"version": str(policy.get("version", "")), "sha256": str(policy.get("sha256", ""))},
            signal_reference_price=float(value["signal_reference_price"]),
            execution_reference_price=float(value["execution_reference_price"]),
            data_cutoff=data_cutoff,
            order=order,
        )
