"""Publish a deterministic next-session trading decision."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from hashlib import sha256
import json

import pandas as pd

from czsc_trader.baseline_execution import apply_resolved_baseline
from czsc_trader.baselines import ResolvedBaseline, resolve_baseline
from czsc_trader.data import load_execution_manifest, load_execution_prices, load_market_data
from czsc_trader.execution_policies import ResolvedExecutionPolicy
from czsc_trader.execution_policy import floor_to_tick, round_to_tick
from czsc_trader.factors import generate_factor_frame

from .context import RepositoryContext
from .errors import ExecutionError, UsageError
from .results import CommandResult


@dataclass(frozen=True)
class AdviceCommand:
    symbol: str
    asset_type: str
    actual_quantity: int
    position_size: int | None = None
    available_cash: float | None = None
    baseline: str | None = None
    cycle_target_quantity: int | None = None


def validate_quantity_input(actual_quantity: int, position_size: int) -> None:
    if actual_quantity < 0:
        raise ValueError("actual quantity must be non-negative")
    if position_size <= 0:
        raise ValueError("position size must be positive")
    if actual_quantity % 100 != 0 or position_size % 100 != 0:
        raise ValueError("quantities must use 100-share lots")


def _validate_account_input(actual_position: int, quantity: int) -> None:
    if actual_position not in (0, 1):
        raise ValueError("actual position must be 0 or 1")
    validate_quantity_input(actual_position * quantity, quantity)


def _decision_id(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "DEC-" + sha256(canonical.encode("utf-8")).hexdigest()[:20].upper()


def build_advice_v1(
    *,
    symbol: str,
    signal_date: pd.Timestamp,
    valid_session: pd.Timestamp,
    signal_close: float,
    execution_close: float,
    target_position: int,
    actual_quantity: int,
    position_size: int,
    policy: ResolvedExecutionPolicy,
    baseline_version: str,
    baseline_sha256: str,
) -> dict[str, object]:
    """Map a binary strategy target and filled shares to a broker-ready decision."""
    validate_quantity_input(actual_quantity, position_size)
    if target_position not in (0, 1):
        raise ValueError("target position must be 0 or 1")
    signal_day = pd.Timestamp(signal_date).normalize()
    valid_day = pd.Timestamp(valid_session).normalize()
    if valid_day <= signal_day:
        raise ValueError("valid session must be after signal date")
    target_quantity = position_size if target_position else 0
    delta_quantity = target_quantity - actual_quantity
    order: dict[str, object] | None = None
    if delta_quantity > 0:
        limit_price = floor_to_tick(
            float(execution_close) * (1.0 + policy.parameter), policy.tick
        )
        order = {
            "side": "BUY",
            "quantity": delta_quantity,
            "order_type": "LIMIT",
            "limit_price": limit_price,
            "time_in_force": "DAY",
        }
        action = "BUY"
    elif delta_quantity < 0:
        if policy.exit_price_rounding != "nearest_half_up":
            raise ValueError(f"unsupported exit price rounding: {policy.exit_price_rounding}")
        limit_price = round_to_tick(
            float(execution_close) * (1.0 - policy.exit_limit_ratio), policy.tick
        )
        order = {
            "side": "SELL",
            "quantity": -delta_quantity,
            "order_type": "LIMIT",
            "limit_price": limit_price,
            "time_in_force": "DAY",
        }
        action = "SELL"
    else:
        action = "HOLD" if target_quantity else "WAIT"
    identity = {
        "contract_version": "advice.v1",
        "symbol": symbol.upper(),
        "signal_date": str(signal_day.date()),
        "valid_session": str(valid_day.date()),
        "actual_quantity": actual_quantity,
        "target_quantity": target_quantity,
        "position_size": position_size,
        "baseline": {"version": baseline_version, "sha256": baseline_sha256},
        "execution_policy": {"version": policy.version, "sha256": policy.sha256},
        "order": order,
    }
    return {
        **identity,
        "decision_id": _decision_id(identity),
        "delta_quantity": delta_quantity,
        "target_position": target_position,
        "action": action,
        "signal_reference_price": float(signal_close),
        "execution_reference_price": float(execution_close),
        "execution_price_adjustment": "none",
    }


def build_advice_v2(
    *, symbol: str, signal_date: pd.Timestamp, valid_session: pd.Timestamp,
    signal_close: float, execution_close: float, target_position: int,
    actual_quantity: int, available_cash: float, policy: ResolvedExecutionPolicy,
    baseline_version: str, baseline_sha256: str,
) -> dict[str, object]:
    """Build a full-cash decision while keeping price and fees project-owned."""
    if actual_quantity < 0 or actual_quantity % 100:
        raise ValueError("actual quantity must use non-negative 100-share lots")
    cash = Decimal(str(available_cash)).quantize(Decimal("0.01"))
    if not cash.is_finite() or cash < 0:
        raise ValueError("available cash must be non-negative and finite")
    if target_position not in (0, 1):
        raise ValueError("target position must be 0 or 1")
    signal_day = pd.Timestamp(signal_date).normalize()
    valid_day = pd.Timestamp(valid_session).normalize()
    if valid_day <= signal_day:
        raise ValueError("valid session must be after signal date")
    fee = Decimal(str(policy.fee_rate))
    order: dict[str, object] | None = None
    estimated = Decimal("0")
    if target_position:
        price = floor_to_tick(float(execution_close) * (1 + policy.parameter), policy.tick)
        unit_cost = Decimal(str(price)) * (Decimal("1") + fee)
        lots = (cash / (unit_cost * 100)).to_integral_value(rounding=ROUND_FLOOR)
        delta = int(lots) * 100
        target = actual_quantity + delta
        if delta:
            order = {"side": "BUY", "quantity": delta, "order_type": "LIMIT",
                     "limit_price": price, "time_in_force": "DAY"}
            estimated = unit_cost * delta
            action = "BUY"
        else:
            action = "HOLD" if actual_quantity else "WAIT"
    else:
        target = 0
        delta = -actual_quantity
        if actual_quantity:
            price = round_to_tick(
                float(execution_close) * (1 - policy.exit_limit_ratio), policy.tick
            )
            order = {"side": "SELL", "quantity": actual_quantity, "order_type": "LIMIT",
                     "limit_price": price, "time_in_force": "DAY"}
            action = "SELL"
        else:
            action = "WAIT"
    identity = {
        "contract_version": "advice.v2", "symbol": symbol.upper(),
        "signal_date": str(signal_day.date()), "valid_session": str(valid_day.date()),
        "actual_quantity": actual_quantity, "target_quantity": target,
        "position_size": target, "available_cash": float(cash),
        "baseline": {"version": baseline_version, "sha256": baseline_sha256},
        "execution_policy": {"version": policy.version, "sha256": policy.sha256},
        "order": order,
    }
    return {
        **identity, "decision_id": _decision_id(identity), "delta_quantity": delta,
        "target_position": target_position, "action": action,
        "signal_reference_price": float(signal_close),
        "execution_reference_price": float(execution_close),
        "execution_price_adjustment": "none", "fee_rate": float(fee),
        "estimated_order_cost": float(estimated.quantize(Decimal("0.01"))),
        "unallocated_cash": float((cash - estimated).quantize(Decimal("0.01"))),
    }


def build_advice_v3(
    *,
    baseline: ResolvedBaseline,
    signal_date: pd.Timestamp,
    valid_session: pd.Timestamp,
    signal_close: float,
    execution_close: float,
    target_position: int,
    actual_quantity: int,
    available_cash: float,
    cycle_target_quantity: int | None,
) -> dict[str, object]:
    """Build an order from one complete baseline and a stable entry-cycle target."""
    execution = baseline.execution
    if execution is None:
        raise ValueError("advice.v3 requires a complete baseline")
    instrument = execution.instrument
    if actual_quantity < 0 or actual_quantity % instrument.lot_size:
        raise ValueError("actual quantity must use complete-baseline lots")
    if cycle_target_quantity is not None and (
        cycle_target_quantity < 0 or cycle_target_quantity % instrument.lot_size
    ):
        raise ValueError("cycle target quantity must use complete-baseline lots")
    if target_position not in (0, 1):
        raise ValueError("target position must be 0 or 1")
    signal_day = pd.Timestamp(signal_date).normalize()
    valid_day = pd.Timestamp(valid_session).normalize()
    if valid_day <= signal_day:
        raise ValueError("valid session must be after signal date")
    cash = Decimal(str(available_cash)).quantize(Decimal("0.01"))
    if not cash.is_finite() or cash < 0:
        raise ValueError("available cash must be non-negative and finite")
    fee = Decimal(str(execution.capital.fee_rate))
    if target_position:
        price = floor_to_tick(
            float(execution_close) * (1 + execution.entry_limit_parameter),
            instrument.price_tick,
        )
        if cycle_target_quantity is None:
            unit_cost = Decimal(str(price)) * (Decimal("1") + fee)
            lots = (cash / (unit_cost * instrument.lot_size)).to_integral_value(
                rounding=ROUND_FLOOR
            )
            target = actual_quantity + int(lots) * instrument.lot_size
        else:
            target = cycle_target_quantity
        delta = max(0, target - actual_quantity)
        side = "BUY"
        action = "BUY" if delta else ("HOLD" if actual_quantity else "WAIT")
    else:
        price = round_to_tick(
            float(execution_close) * (1 - execution.exit_limit_ratio),
            instrument.price_tick,
        )
        target = 0
        delta = -actual_quantity
        side = "SELL"
        action = "SELL" if actual_quantity else "WAIT"
    orders: list[dict[str, object]] = []
    remaining = abs(delta)
    while remaining:
        quantity = min(remaining, instrument.maximum_order_quantity)
        orders.append(
            {
                "side": side,
                "quantity": quantity,
                "order_type": "LIMIT",
                "limit_price": price,
                "time_in_force": "DAY",
            }
        )
        remaining -= quantity
    identity = {
        "contract_version": "advice.v3",
        "symbol": instrument.symbol,
        "signal_date": str(signal_day.date()),
        "valid_session": str(valid_day.date()),
        "actual_quantity": actual_quantity,
        "cycle_target_quantity": target if target_position else (0 if not actual_quantity else cycle_target_quantity),
        "target_quantity": target,
        "baseline": {"version": baseline.version, "sha256": baseline.sha256},
        "orders": orders,
    }
    return {
        **identity,
        "decision_id": _decision_id(identity),
        "delta_quantity": delta,
        "target_position": target_position,
        "action": action,
        "signal_reference_price": float(signal_close),
        "execution_reference_price": float(execution_close),
        "execution_price_adjustment": "none",
        "order": orders[0] if len(orders) == 1 else None,
        "available_cash": float(cash),
        "fee_rate": float(fee),
    }


def build_advice(
    *,
    signal_date: pd.Timestamp,
    valid_session: pd.Timestamp,
    signal_close: float,
    execution_close: float,
    target_position: int,
    actual_position: int,
    quantity: int,
    policy: ResolvedExecutionPolicy,
) -> dict[str, object]:
    """Backward-compatible human advice used by existing callers."""
    _validate_account_input(actual_position, quantity)
    result = build_advice_v1(
        symbol=policy.symbol,
        signal_date=signal_date,
        valid_session=valid_session,
        signal_close=signal_close,
        execution_close=execution_close,
        target_position=target_position,
        actual_quantity=actual_position * quantity,
        position_size=quantity,
        policy=policy,
        baseline_version=policy.baseline_version,
        baseline_sha256=policy.baseline_sha256,
    )
    state_by_action = {
        "WAIT": "CASH",
        "HOLD": "HOLDING",
        "BUY": "PENDING_ENTRY",
        "SELL": "PENDING_EXIT",
    }
    legacy_order = None
    if result["action"] == "BUY":
        assert result["order"] is not None
        price = float(result["order"]["limit_price"])
        warning = floor_to_tick(
            float(execution_close) * (1.0 + policy.warning_gap_q05), policy.tick
        )
        legacy_order = {
            "order_type": "限价委托",
            "maximum_buy_price": price,
            "low_open_warning_price": warning,
            "valid_for": "NEXT_TRADING_SESSION",
            "instruction": f"买入{quantity}份；委托价不得高于{price:.3f}元；当日未成交则失效。",
        }
    elif result["action"] == "SELL":
        assert result["order"] is not None
        price = float(result["order"]["limit_price"])
        legacy_order = {
            "primary_order_type": "限价委托",
            "limit_price": price,
            "valid_for": "NEXT_TRADING_SESSION",
            "instruction": f"以{price:.3f}元限价卖出{quantity}份；未收到明确成交回报前继续按持仓处理。",
        }
    return {
        "signal_date": result["signal_date"],
        "valid_for": "NEXT_TRADING_SESSION",
        "target_position": target_position,
        "actual_position": actual_position,
        "quantity": quantity,
        "signal_reference_price": float(signal_close),
        "execution_reference_price": float(execution_close),
        "execution_price_adjustment": "none",
        "state": state_by_action[str(result["action"])],
        "action": result["action"],
        "order": legacy_order,
    }


def run_advice(context: RepositoryContext, request: AdviceCommand) -> CommandResult:
    """Generate advice from the latest complete locally tracked close."""
    try:
        if request.available_cash is None:
            raise ValueError("complete-baseline advice requires available cash")
    except ValueError as exc:
        raise UsageError("invalid_account_state", str(exc)) from exc
    try:
        baseline = resolve_baseline(
            context.baseline_root, request.baseline, symbol=request.symbol
        )
        if baseline.execution is None:
            raise ValueError("selected baseline does not contain execution rules")
        data = load_market_data(context.raw_dir, request.symbol, request.asset_type)
        execution_prices = load_execution_prices(
            context.raw_dir, request.symbol, request.asset_type
        )
        execution_manifest = load_execution_manifest(
            context.raw_dir, request.symbol, request.asset_type
        )
        factor_result = generate_factor_frame(data)
        close = pd.Series(
            data.daily["close"].astype(float).to_numpy(),
            index=pd.DatetimeIndex(pd.to_datetime(data.daily["dt"]), name="dt"),
            name="close",
        )
        applied = apply_resolved_baseline(factor_result.frame, baseline, daily_close=close)
        signal_date = pd.Timestamp(close.index[-1])
        execution_rows = execution_prices.loc[execution_prices["dt"].eq(signal_date)]
        if len(execution_rows) != 1:
            raise ValueError(
                f"execution price must contain exactly one row for {signal_date.date()}"
            )
        advice = build_advice_v3(
            baseline=baseline,
            signal_date=signal_date,
            valid_session=pd.Timestamp(execution_manifest["next_trading_session"]),
            signal_close=float(close.loc[signal_date]),
            execution_close=float(execution_rows.iloc[0]["close"]),
            target_position=int(applied.target_position.loc[signal_date]),
            actual_quantity=request.actual_quantity,
            available_cash=float(request.available_cash),
            cycle_target_quantity=request.cycle_target_quantity,
        )
        result = {
            **advice,
            "data_cutoff": str(signal_date.date()),
            "factor_score": float(applied.scores.loc[signal_date]),
            "confirmation_rule": "未收到明确成交回报时，实际持仓数量保持不变。",
            "scope": "机器可读交易决策；订单执行由独立运行引擎负责。",
        }
    except UsageError:
        raise
    except Exception as exc:
        raise ExecutionError(
            "advice_failed",
            str(exc),
            context={"symbol": request.symbol, "baseline": request.baseline},
        ) from exc
    return CommandResult(status="PASS", command="advice.run", result=result)
