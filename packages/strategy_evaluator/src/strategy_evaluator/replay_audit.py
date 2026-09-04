from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from hashlib import sha256
import json
from typing import Any, Mapping

from .audit_models import AuditStatus
from .models import Record


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


@dataclass(frozen=True)
class ReplayEvidence(Record):
    strategy_hash: str
    data_hash: str
    initial_cash: float
    execution_spec: Mapping[str, Any]
    decisions: tuple[Mapping[str, Any], ...]
    orders: tuple[Mapping[str, Any], ...]
    fills: tuple[Mapping[str, Any], ...]
    account_daily: tuple[Mapping[str, Any], ...]
    trades: tuple[Mapping[str, Any], ...]
    execution_daily: tuple[Mapping[str, Any], ...]
    execution_intraday: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return _jsonable({name: getattr(self, name) for name in self.__dataclass_fields__})


@dataclass(frozen=True)
class ReplayAuditResult(Record):
    status: AuditStatus
    evidence_hash: str
    checks: tuple[str, ...]
    reason_codes: tuple[str, ...] = ()


def hash_replay_evidence(evidence: ReplayEvidence) -> str:
    encoded = json.dumps(
        evidence.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _floor(price: float, tick: float) -> float:
    return float(
        (Decimal(str(price)) / Decimal(str(tick))).to_integral_value(rounding=ROUND_FLOOR)
        * Decimal(str(tick))
    )


def _nearest(price: float, tick: float) -> float:
    return float(
        (Decimal(str(price)) / Decimal(str(tick))).to_integral_value(rounding=ROUND_HALF_UP)
        * Decimal(str(tick))
    )


def audit_replay(evidence: ReplayEvidence, tolerance: float = 1e-7) -> ReplayAuditResult:
    """Independently recompute causal order, fill, and account invariants."""
    digest = hash_replay_evidence(evidence)
    reasons: list[str] = []
    checks: list[str] = []
    spec = evidence.execution_spec
    instrument = spec["instrument"]
    fee_rate = float(spec["fee_rate"])
    lot_size = int(instrument["lot_size"])
    tick = float(instrument["price_tick"])
    daily = {str(row["date"])[:10]: row for row in evidence.execution_daily}
    intraday_by_day: dict[str, list[Mapping[str, Any]]] = {}
    for row in evidence.execution_intraday:
        intraday_by_day.setdefault(str(row["time"])[:10], []).append(row)
    decisions = {str(row["decision_id"]): row for row in evidence.decisions}
    if len(decisions) != len(evidence.decisions):
        reasons.append("DUPLICATE_DECISION_ID")
    fills_by_order = {str(row["order_id"]): row for row in evidence.fills}
    if len(fills_by_order) != len(evidence.fills):
        reasons.append("DUPLICATE_ORDER_FILL")
    order_ids: set[str] = set()
    for order in evidence.orders:
        order_id = str(order["order_id"])
        if order_id in order_ids:
            reasons.append("DUPLICATE_ORDER_ID")
            continue
        order_ids.add(order_id)
        decision = decisions.get(str(order["decision_id"]))
        if decision is None or str(decision["valid_session"])[:10] != str(order["execution_date"])[:10]:
            reasons.append("ORDER_DECISION_MISMATCH")
            continue
        quantity = int(order["quantity"])
        if quantity <= 0 or quantity % lot_size:
            reasons.append("INVALID_ORDER_LOT")
        signal = daily.get(str(order["signal_date"])[:10])
        execution = daily.get(str(order["execution_date"])[:10])
        if signal is None or execution is None:
            reasons.append("MISSING_EXECUTION_PRICE")
            continue
        if order["side"] == "BUY":
            expected_limit = _floor(
                float(signal["close"]) * (1 + float(spec["entry_limit_parameter"])), tick
            )
        else:
            expected_limit = _nearest(
                float(signal["close"]) * (1 - float(spec["exit_limit_ratio"])), tick
            )
        if abs(float(order["limit_price"]) - expected_limit) > tolerance:
            reasons.append("ORDER_LIMIT_MISMATCH")
        fill = fills_by_order.get(order_id)
        if str(order["status"]) == "FILLED" and fill is None:
            reasons.append("FILLED_ORDER_WITHOUT_FILL")
            continue
        if str(order["status"]) == "UNFILLED" and fill is not None:
            reasons.append("UNFILLED_ORDER_HAS_FILL")
            continue
        if fill is None:
            continue
        if int(fill["quantity"]) != quantity or str(fill["side"]) != str(order["side"]):
            reasons.append("FILL_ORDER_MISMATCH")
        price = float(fill["price"])
        trigger = str(fill["trigger"])
        if order["side"] == "SELL":
            eligible = trigger == "OPEN" and abs(price - float(execution["open"])) <= tolerance
        elif trigger == "OPEN":
            eligible = (
                float(execution["open"]) <= expected_limit + tolerance
                and abs(price - float(execution["open"])) <= tolerance
            )
        else:
            bars = intraday_by_day.get(str(order["execution_date"])[:10], [])
            eligible = trigger == "INTRADAY_LIMIT" and any(
                float(bar["low"]) < expected_limit for bar in bars
            ) and abs(price - expected_limit) <= tolerance
        if not eligible:
            reasons.append("INVALID_FILL_TRIGGER")
        if abs(float(fill["fees"]) - quantity * price * fee_rate) > tolerance:
            reasons.append("FEE_MISMATCH")
    checks.append("ORDER_AND_FILL_CAUSALITY")

    cash = float(evidence.initial_cash)
    quantity = 0
    fills_by_day: dict[str, list[Mapping[str, Any]]] = {}
    for fill in evidence.fills:
        fills_by_day.setdefault(str(fill["fill_time"])[:10], []).append(fill)
    previous_date = ""
    for row in evidence.account_daily:
        day = str(row["date"])[:10]
        if day <= previous_date:
            reasons.append("INVALID_ACCOUNT_INDEX")
        previous_date = day
        if abs(float(row["cash_before"]) - cash) > tolerance or int(row["quantity_before"]) != quantity:
            reasons.append("ACCOUNT_OPENING_STATE_MISMATCH")
        for fill in fills_by_day.get(day, []):
            gross = int(fill["quantity"]) * float(fill["price"])
            fee = float(fill["fees"])
            if fill["side"] == "BUY":
                cash -= gross + fee
                quantity += int(fill["quantity"])
            else:
                cash += gross - fee
                quantity -= int(fill["quantity"])
        expected_equity = cash + quantity * float(row["close"])
        if (
            abs(float(row["cash"]) - cash) > tolerance
            or int(row["quantity"]) != quantity
            or abs(float(row["equity"]) - expected_equity) > tolerance
            or cash < -tolerance
            or quantity < 0
        ):
            reasons.append("ACCOUNT_LEDGER_MISMATCH")
    checks.append("ACCOUNT_LEDGER")
    unique_reasons = tuple(dict.fromkeys(reasons))
    return ReplayAuditResult(
        AuditStatus.FAIL if unique_reasons else AuditStatus.PASS,
        digest,
        tuple(checks),
        unique_reasons,
    )
