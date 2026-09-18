"""Publish a deterministic next-session trading decision."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from hashlib import sha256
import json

import pandas as pd

from czsc_trader.baselines import ResolvedBaseline, resolve_strategy_payload
from czsc_trader.causal_feature_gate_runtime import latest_signal as latest_causal_signal
from czsc_trader.constituent_moneyflow_runtime import latest_breadth_signal
from czsc_trader.data import load_execution_manifest, load_execution_prices, load_market_data
from czsc_trader.execution_policies import ResolvedExecutionPolicy
from czsc_trader.execution_policy import floor_to_tick, round_to_tick
from czsc_trader.execution_intent import decide_order_intent
from czsc_trader.strategy_runtime import apply_resolved_strategy

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
    strategy: str | None = None
    strategy_version: str | None = None
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
    signal_day = pd.Timestamp(signal_date).normalize()
    valid_day = pd.Timestamp(valid_session).normalize()
    if valid_day <= signal_day:
        raise ValueError("valid session must be after signal date")
    cash = Decimal(str(available_cash)).quantize(Decimal("0.01"))
    if not cash.is_finite() or cash < 0:
        raise ValueError("available cash must be non-negative and finite")
    fee = Decimal(str(execution.capital.fee_rate))
    intent = decide_order_intent(
        target_position=target_position,
        actual_quantity=actual_quantity,
        cycle_target_quantity=cycle_target_quantity,
        available_cash=float(cash),
        execution_close=execution_close,
        execution_spec=execution,
    )
    target = intent.target_quantity
    delta = intent.delta_quantity
    action = intent.action
    orders = [
        {
            "side": order.side,
            "quantity": order.quantity,
            "order_type": order.order_type,
            "limit_price": order.limit_price,
            "time_in_force": "DAY",
        }
        for order in intent.orders
    ]
    estimated = sum(
        (
            Decimal(order.quantity)
            * Decimal(str(order.limit_price))
            * (Decimal("1") + fee)
            for order in intent.orders
            if order.side == "BUY"
        ),
        start=Decimal("0"),
    )
    identity = {
        "contract_version": "advice.v3",
        "symbol": instrument.symbol,
        "signal_date": str(signal_day.date()),
        "valid_session": str(valid_day.date()),
        "actual_quantity": actual_quantity,
        "cycle_target_quantity": intent.cycle_target_quantity,
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
        "estimated_order_cost": float(estimated.quantize(Decimal("0.01"))),
        "unallocated_cash": float((cash - estimated).quantize(Decimal("0.01"))),
        "capital_rule": {
            "mode": execution.capital.mode,
            "allocation_fraction": execution.capital.allocation_fraction,
            "target_scope": execution.capital.target_scope,
        },
    }


def build_advice_v4(
    *,
    strategy: dict[str, object],
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
    """Build a complete-baseline order carrying the formal strategy release identity."""
    required = {
        "strategy_id",
        "name",
        "version",
        "release_id",
        "release_hash",
        "qualification",
    }
    if set(strategy) != required:
        raise ValueError("advice.v4 strategy identity fields are incomplete")
    legacy = build_advice_v3(
        baseline=baseline,
        signal_date=signal_date,
        valid_session=valid_session,
        signal_close=signal_close,
        execution_close=execution_close,
        target_position=target_position,
        actual_quantity=actual_quantity,
        available_cash=available_cash,
        cycle_target_quantity=cycle_target_quantity,
    )
    immutable_strategy = {
        "strategy_id": strategy["strategy_id"],
        "version": strategy["version"],
        "release_hash": strategy["release_hash"],
    }
    decision_identity = {
        "contract_version": "advice.v4",
        "symbol": legacy["symbol"],
        "signal_date": legacy["signal_date"],
        "valid_session": legacy["valid_session"],
        "actual_quantity": legacy["actual_quantity"],
        "cycle_target_quantity": legacy["cycle_target_quantity"],
        "target_quantity": legacy["target_quantity"],
        "strategy": immutable_strategy,
        "orders": legacy["orders"],
    }
    display = dict(legacy)
    display.pop("contract_version")
    display.pop("baseline")
    display.pop("decision_id")
    return {
        **display,
        "contract_version": "advice.v4",
        "strategy": dict(strategy),
        "decision_id": _decision_id(decision_identity),
    }


def build_intraday_overlay_advice_v5(
    *,
    strategy: dict[str, object],
    baseline: ResolvedBaseline,
    signal_date: pd.Timestamp,
    valid_session: pd.Timestamp,
    signal_close: float,
    execution_close: float,
    actual_quantity: int,
    available_cash: float,
    cycle_target_quantity: int | None,
    event_triggered: bool,
) -> dict[str, object]:
    """Build a durable open-entry/11:30-exit plan for the S003 overlay contract."""
    spec = baseline.constituent_moneyflow_intraday
    if spec is None or baseline.strategy != "constituent_moneyflow_intraday_overlay":
        raise ValueError("intraday overlay advice requires a complete overlay strategy")
    if actual_quantity < 0 or actual_quantity % spec.lot_size:
        raise ValueError("intraday overlay actual quantity violates lot size")
    signal_day = pd.Timestamp(signal_date).normalize()
    valid_day = pd.Timestamp(valid_session).normalize()
    if valid_day <= signal_day:
        raise ValueError("intraday overlay valid session must follow signal date")
    fee_rate = Decimal("0.0005")
    cash = Decimal(str(available_cash))
    if cash < 0:
        raise ValueError("intraday overlay available cash must be non-negative")
    signal_price = Decimal(str(signal_close))
    execution_price = Decimal(str(execution_close))
    if not signal_price.is_finite() or signal_price <= 0:
        raise ValueError("intraday overlay signal close must be positive and finite")
    if not execution_price.is_finite() or execution_price <= 0:
        raise ValueError("intraday overlay execution close must be positive and finite")
    # OPEN is implemented as a strategy-owned marketable limit. PTE passes this exact
    # value to Futu with adjust_limit disabled; the broker never invents the price.
    buy_limit = Decimal(str(floor_to_tick(float(execution_price) * 1.10, 0.001)))

    def affordable(budget: Decimal) -> int:
        raw = int(budget / (buy_limit * (Decimal("1") + fee_rate)))
        return raw // spec.lot_size * spec.lot_size

    plan_mode = "NONE"
    action = "HOLD"
    target_quantity = actual_quantity
    cycle_target = cycle_target_quantity or 0
    plan_legs: list[dict[str, object]] = []
    reserve = Decimal("0")
    if cycle_target_quantity is None:
        if actual_quantity != 0:
            raise ValueError("intraday overlay core identity is missing for a non-flat account")
        core_quantity = affordable(cash * Decimal(str(spec.core_fraction)))
        if core_quantity <= 0:
            raise ValueError("intraday overlay account cannot afford one core lot")
        target_quantity = core_quantity
        cycle_target = core_quantity
        action = "BUY"
        plan_mode = "CORE_SETUP"
        reserve = buy_limit * core_quantity * (Decimal("1") + fee_rate)
        plan_legs = [{
            "sequence": 0,
            "role": "CORE_SETUP",
            "checkpoint": "OPEN",
            "submit_after": "09:30:00",
            "submit_before": "09:35:00",
            "dependency_sequence": None,
            "dependency_required_status": None,
            "order": {
                "side": "BUY", "quantity": core_quantity, "order_type": "LIMIT",
                "limit_price": float(buy_limit), "time_in_force": "DAY",
            },
        }]
    else:
        if cycle_target_quantity <= 0 or cycle_target_quantity % spec.lot_size:
            raise ValueError("intraday overlay core quantity is invalid")
        if actual_quantity != cycle_target_quantity:
            raise ValueError("intraday overlay account differs from its settled core quantity")
        if event_triggered:
            event_quantity = min(cycle_target_quantity, affordable(cash))
            if event_quantity <= 0:
                raise ValueError("intraday overlay event cash cannot afford one lot")
            action = "ROTATE"
            plan_mode = "CORE_EVENT_INTRADAY_ROTATION"
            reserve = buy_limit * event_quantity * (Decimal("1") + fee_rate)
            plan_legs = [
                {
                    "sequence": 0,
                    "role": "ROTATION_ENTRY",
                    "checkpoint": "OPEN",
                    "submit_after": "09:30:00",
                    "submit_before": "09:35:00",
                    "dependency_sequence": None,
                    "dependency_required_status": None,
                    "order": {
                        "side": "BUY", "quantity": event_quantity,
                        "order_type": "LIMIT", "limit_price": float(buy_limit),
                        "time_in_force": "DAY",
                    },
                },
                {
                    "sequence": 1,
                    "role": "ROTATION_EXIT",
                    "checkpoint": "11:30_CLOSE",
                    "submit_after": "11:29:00",
                    "submit_before": "11:30:00",
                    "dependency_sequence": 0,
                    "dependency_required_status": "FILLED_ALL",
                    "order": {
                        "side": "SELL", "quantity": event_quantity,
                        "order_type": "MARKET", "limit_price": float(signal_close),
                        "time_in_force": "DAY",
                    },
                },
            ]
    delta = target_quantity - actual_quantity
    identity = {
        "contract_version": "advice.v5",
        "symbol": spec.symbol,
        "signal_date": str(signal_day.date()),
        "valid_session": str(valid_day.date()),
        "actual_quantity": actual_quantity,
        "cycle_target_quantity": cycle_target,
        "target_quantity": target_quantity,
        "strategy": {
            "strategy_id": strategy["strategy_id"],
            "version": strategy["version"],
            "release_hash": strategy["release_hash"],
        },
        "plan_mode": plan_mode,
        "plan_legs": plan_legs,
    }
    return {
        **identity,
        "decision_id": _decision_id(identity),
        "delta_quantity": delta,
        "action": action,
        "strategy": dict(strategy),
        "signal_reference_price": float(signal_close),
        "execution_reference_price": float(execution_close),
        "order": None,
        "orders": [],
        "available_cash": float(cash),
        "fee_rate": float(fee_rate),
        "estimated_order_cost": float(reserve.quantize(Decimal("0.0001"))),
        "unallocated_cash": float((cash - reserve).quantize(Decimal("0.0001"))),
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
        from strategy_manager import StrategyRegistry

        if request.strategy and request.baseline:
            raise ValueError("use either strategy or legacy baseline, not both")
        registry = StrategyRegistry(context.strategy_root)
        reference = request.strategy or request.baseline or "S001"
        release = registry.resolve_strategy(reference, request.strategy_version)
        registry.assert_deployable(release.strategy_id, release.version, "PAPER")
        strategy = registry.get_family(release.strategy_id)
        if release.release_hash is None:
            raise ValueError("deployable strategy version must have a release hash")
        baseline = resolve_strategy_payload(
            context.strategy_dependency_root,
            release.strategy_payload,
            release_id=release.release_id,
            release_hash=release.release_hash,
            symbol=request.symbol,
            repository_root=context.root,
        )
        strategy_identity = {
            "strategy_id": strategy.strategy_id,
            "name": strategy.name,
            "version": release.version,
            "release_id": release.release_id,
            "release_hash": release.release_hash,
            "qualification": registry.current_qualification(
                release.strategy_id, release.version
            ).value,
        }
        data = load_market_data(context.raw_dir, request.symbol, request.asset_type)
        execution_prices = load_execution_prices(
            context.raw_dir, request.symbol, request.asset_type
        )
        execution_manifest = load_execution_manifest(
            context.raw_dir, request.symbol, request.asset_type
        )
        close = pd.Series(
            data.daily["close"].astype(float).to_numpy(),
            index=pd.DatetimeIndex(pd.to_datetime(data.daily["dt"]), name="dt"),
            name="close",
        )
        signal_date = pd.Timestamp(close.index[-1])
        execution_rows = execution_prices.loc[execution_prices["dt"].eq(signal_date)]
        if len(execution_rows) != 1:
            raise ValueError(
                f"execution price must contain exactly one row for {signal_date.date()}"
            )
        if baseline.strategy == "constituent_moneyflow_intraday_overlay":
            spec = baseline.constituent_moneyflow_intraday
            if spec is None:
                raise ValueError("intraday overlay strategy specification is missing")
            triggered, breadth, threshold, coverage = latest_breadth_signal(
                context.raw_dir,
                str(strategy_identity["release_id"]),
                spec,
                signal_date,
            )
            advice = build_intraday_overlay_advice_v5(
                strategy=strategy_identity,
                baseline=baseline,
                signal_date=signal_date,
                valid_session=pd.Timestamp(execution_manifest["next_trading_session"]),
                signal_close=float(close.loc[signal_date]),
                execution_close=float(execution_rows.iloc[0]["close"]),
                actual_quantity=request.actual_quantity,
                available_cash=float(request.available_cash),
                cycle_target_quantity=request.cycle_target_quantity,
                event_triggered=triggered,
            )
            factor_score = breadth
            feature_evidence = {
                "moneyflow_breadth": breadth,
                "prior_threshold": threshold,
                "observed_weight_ratio": coverage,
                "event_triggered": triggered,
            }
        elif baseline.strategy == "causal_feature_gate":
            spec = baseline.causal_feature_gate
            if spec is None:
                raise ValueError("causal-feature strategy specification is missing")
            target_position, factor_score, confirmation_score, raw_features = (
                latest_causal_signal(
                    context.raw_dir,
                    str(strategy_identity["release_id"]),
                    spec,
                    signal_date,
                )
            )
            advice = build_advice_v4(
                strategy=strategy_identity,
                baseline=baseline,
                signal_date=signal_date,
                valid_session=pd.Timestamp(execution_manifest["next_trading_session"]),
                signal_close=float(close.loc[signal_date]),
                execution_close=float(execution_rows.iloc[0]["close"]),
                target_position=target_position,
                actual_quantity=request.actual_quantity,
                available_cash=float(request.available_cash),
                cycle_target_quantity=request.cycle_target_quantity,
            )
            feature_evidence = {
                "base_score": factor_score,
                "confirmation_score": confirmation_score,
                "features": raw_features,
            }
        else:
            applied = apply_resolved_strategy(data, baseline)
            advice = build_advice_v4(
                strategy=strategy_identity,
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
            factor_score = float(applied.scores.loc[signal_date])
            feature_evidence = None
        result = {
            **advice,
            "data_cutoff": str(signal_date.date()),
            "factor_score": factor_score,
            **({"feature_evidence": feature_evidence} if feature_evidence else {}),
            "confirmation_rule": "未收到明确成交回报时，实际持仓数量保持不变。",
            "scope": "机器可读交易决策；订单执行由独立运行引擎负责。",
        }
    except UsageError:
        raise
    except Exception as exc:
        raise ExecutionError(
            "advice_failed",
            str(exc),
            context={
                "symbol": request.symbol,
                "strategy": request.strategy,
                "strategy_version": request.strategy_version,
                "legacy_baseline": request.baseline,
            },
        ) from exc
    return CommandResult(status="PASS", command="advice.run", result=result)
