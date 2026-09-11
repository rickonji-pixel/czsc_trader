from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from hashlib import sha256
import json
from typing import Any, Mapping

import numpy as np
import pandas as pd

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
    metrics: Mapping[str, Any]
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


def _ceil(price: float, tick: float) -> float:
    return float(
        (Decimal(str(price)) / Decimal(str(tick))).to_integral_value(rounding=ROUND_CEILING)
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
    price_limit_ratio = float(instrument["price_limit_ratio"])
    daily = {str(row["date"])[:10]: row for row in evidence.execution_daily}
    intraday_by_day: dict[str, list[Mapping[str, Any]]] = {}
    for row in evidence.execution_intraday:
        intraday_by_day.setdefault(str(row["time"])[:10], []).append(row)
    decisions = {str(row["decision_id"]): row for row in evidence.decisions}
    if len(decisions) != len(evidence.decisions):
        reasons.append("DUPLICATE_DECISION_ID")
    fills_by_order = {str(row["order_id"]): row for row in evidence.fills}
    accounts = {str(row["date"])[:10]: row for row in evidence.account_daily}
    if any(
        len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower())
        for value in (evidence.strategy_hash, evidence.data_hash)
    ):
        reasons.append("INVALID_EVIDENCE_IDENTITY")
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
            expected_order_type = "LIMIT"
            signal_close = float(signal["close"])
            expected_limit = min(
                _floor(signal_close * (1 + float(spec["entry_limit_parameter"])), tick),
                _floor(signal_close * (1 + price_limit_ratio), tick) - tick,
            )
        else:
            expected_order_type = "MARKET"
            signal_close = float(signal["close"])
            expected_limit = _nearest(signal_close, tick)
        if str(order.get("order_type", "LIMIT")) != expected_order_type:
            reasons.append("ORDER_TYPE_MISMATCH")
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
            if order["side"] == "SELL" and expected_order_type == "MARKET":
                reasons.append("MISSED_ELIGIBLE_FILL")
            elif order["side"] == "SELL":
                bars = intraday_by_day.get(str(order["execution_date"])[:10], [])
                eligible_price = (
                    float(execution["open"]) >= expected_limit - tolerance
                    or any(float(bar["high"]) > expected_limit for bar in bars)
                )
                if eligible_price:
                    reasons.append("MISSED_ELIGIBLE_FILL")
            else:
                bars = intraday_by_day.get(str(order["execution_date"])[:10], [])
                eligible_price = (
                    float(execution["open"]) <= expected_limit + tolerance
                    or any(float(bar["low"]) < expected_limit for bar in bars)
                )
                account = accounts.get(str(order["execution_date"])[:10])
                affordable = account is not None and (
                    quantity * expected_limit * (1 + fee_rate)
                    <= float(account["cash_before"]) + tolerance
                )
                if eligible_price and affordable:
                    reasons.append("MISSED_ELIGIBLE_FILL")
            continue
        if int(fill["quantity"]) != quantity or str(fill["side"]) != str(order["side"]):
            reasons.append("FILL_ORDER_MISMATCH")
        price = float(fill["price"])
        trigger = str(fill["trigger"])
        if order["side"] == "SELL" and expected_order_type == "MARKET":
            eligible = (
                trigger == "OPEN_MARKET"
                and abs(price - float(execution["open"])) <= tolerance
            )
        elif order["side"] == "SELL":
            bars = intraday_by_day.get(str(order["execution_date"])[:10], [])
            eligible = (
                trigger == "OPEN"
                and float(execution["open"]) >= expected_limit - tolerance
                and abs(price - float(execution["open"])) <= tolerance
            ) or (
                trigger == "INTRADAY_LIMIT"
                and any(float(bar["high"]) > expected_limit for bar in bars)
                and abs(price - expected_limit) <= tolerance
            )
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

    expected_trades: dict[str, dict[str, Any]] = {}
    fills_by_cycle: dict[str, list[Mapping[str, Any]]] = {}
    for fill in evidence.fills:
        fills_by_cycle.setdefault(str(fill["cycle_id"]), []).append(fill)
    for cycle_id, cycle_fills in fills_by_cycle.items():
        buys = [item for item in cycle_fills if item["side"] == "BUY"]
        sells = [item for item in cycle_fills if item["side"] == "SELL"]
        buy_quantity = sum(int(item["quantity"]) for item in buys)
        sell_quantity = sum(int(item["quantity"]) for item in sells)
        buy_gross = sum(int(item["quantity"]) * float(item["price"]) for item in buys)
        sell_gross = sum(int(item["quantity"]) * float(item["price"]) for item in sells)
        buy_fees = sum(float(item["fees"]) for item in buys)
        sell_fees = sum(float(item["fees"]) for item in sells)
        closed = bool(buys and sell_quantity == buy_quantity)
        expected_trades[cycle_id] = {
            "status": "CLOSED" if closed else "OPEN",
            "quantity": buy_quantity,
            "entry_price": buy_gross / buy_quantity if buy_quantity else None,
            "exit_price": sell_gross / sell_quantity if closed else None,
            "net_return": (
                (sell_gross - sell_fees) / (buy_gross + buy_fees) - 1 if closed else None
            ),
        }
    actual_trades = {str(row["cycle_id"]): row for row in evidence.trades}
    if set(actual_trades) != set(expected_trades):
        reasons.append("TRADE_PAIRING_MISMATCH")
    else:
        for cycle_id, expected in expected_trades.items():
            actual = actual_trades[cycle_id]
            if (
                str(actual["status"]) != expected["status"]
                or int(actual["quantity"]) != expected["quantity"]
            ):
                reasons.append("TRADE_PAIRING_MISMATCH")
                continue
            for name in ("entry_price", "exit_price", "net_return"):
                expected_value = expected[name]
                actual_value = actual.get(name)
                if expected_value is None:
                    if actual_value is not None:
                        reasons.append("TRADE_VALUE_MISMATCH")
                elif actual_value is None or abs(float(actual_value) - float(expected_value)) > tolerance:
                    reasons.append("TRADE_VALUE_MISMATCH")
    checks.append("TRADE_PAIRING")

    equity_series = pd.Series(
        [float(row["equity"]) for row in evidence.account_daily], dtype=float
    )
    total_return = equity_series.iloc[-1] / evidence.initial_cash - 1
    max_drawdown = float(equity_series.div(equity_series.cummax()).sub(1).min())
    annualized = float(
        (equity_series.iloc[-1] / evidence.initial_cash) ** (252 / len(equity_series)) - 1
    )
    calmar = annualized / abs(max_drawdown) if abs(max_drawdown) > 1e-12 else None
    previous = equity_series.shift(1)
    previous.iloc[0] = evidence.initial_cash
    returns = equity_series.div(previous).sub(1)
    volatility = float(returns.std(ddof=1))
    sharpe = (
        float(np.sqrt(252) * returns.mean() / volatility)
        if np.isfinite(volatility) and volatility > 0 else None
    )
    closed_returns = [
        float(item["net_return"])
        for item in expected_trades.values()
        if item["status"] == "CLOSED"
    ]
    wins = [value for value in closed_returns if value > 0]
    losses = [value for value in closed_returns if value < 0]
    if not closed_returns:
        ratio, ratio_status = None, "NO_CLOSED_TRADES"
    elif not wins:
        ratio, ratio_status = None, "NO_WINS"
    elif not losses:
        ratio, ratio_status = None, "NO_LOSSES"
    else:
        ratio = (sum(wins) / len(wins)) / abs(sum(losses) / len(losses))
        ratio_status = "VALID"
    expected_metrics = {
        "max_drawdown": max_drawdown,
        "calmar": calmar,
        "win_loss_ratio": ratio,
        "win_loss_ratio_status": ratio_status,
        "return": total_return,
        "sharpe": sharpe,
        "closed_trades": len(closed_returns),
    }
    for name, expected in expected_metrics.items():
        actual = evidence.metrics.get(name)
        if isinstance(expected, (str, int)):
            if actual != expected:
                reasons.append("METRIC_MISMATCH")
        elif expected is None:
            if actual is not None:
                reasons.append("METRIC_MISMATCH")
        elif actual is None or abs(float(actual) - float(expected)) > tolerance:
            reasons.append("METRIC_MISMATCH")
    checks.append("METRICS")
    unique_reasons = tuple(dict.fromkeys(reasons))
    return ReplayAuditResult(
        AuditStatus.FAIL if unique_reasons else AuditStatus.PASS,
        digest,
        tuple(checks),
        unique_reasons,
    )
