"""Generate one stateless next-session manual trading recommendation."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from czsc_trader.baseline_execution import apply_resolved_baseline
from czsc_trader.baselines import resolve_baseline
from czsc_trader.data import load_market_data
from czsc_trader.execution_policies import ResolvedExecutionPolicy, resolve_execution_policy
from czsc_trader.execution_policy import floor_to_tick
from czsc_trader.factors import generate_factor_frame

from .context import RepositoryContext
from .errors import ExecutionError, UsageError
from .results import CommandResult


@dataclass(frozen=True)
class AdviceCommand:
    symbol: str
    asset_type: str
    actual_position: int
    quantity: int
    baseline: str | None = None


def _validate_account_input(actual_position: int, quantity: int) -> None:
    if actual_position not in (0, 1):
        raise ValueError("actual position must be 0 or 1")
    if quantity <= 0 or quantity % 100 != 0:
        raise ValueError("quantity must be positive and use 100-share lots")


def build_advice(
    *,
    signal_date: pd.Timestamp,
    close: float,
    target_position: int,
    actual_position: int,
    quantity: int,
    policy: ResolvedExecutionPolicy,
) -> dict[str, object]:
    """Map one close-known target and explicit actual position to a manual order."""
    _validate_account_input(actual_position, quantity)
    if target_position not in (0, 1):
        raise ValueError("target position must be 0 or 1")
    date_text = str(pd.Timestamp(signal_date).date())
    common: dict[str, object] = {
        "signal_date": date_text,
        "valid_for": "NEXT_TRADING_SESSION",
        "target_position": target_position,
        "actual_position": actual_position,
        "quantity": quantity,
    }
    if target_position == 0 and actual_position == 0:
        return {**common, "state": "CASH", "action": "WAIT", "order": None}
    if target_position == 1 and actual_position == 1:
        return {**common, "state": "HOLDING", "action": "HOLD", "order": None}
    if target_position == 1:
        maximum = floor_to_tick(float(close) * (1.0 + policy.parameter), policy.tick)
        warning = floor_to_tick(float(close) * (1.0 + policy.warning_gap_q05), policy.tick)
        return {
            **common,
            "state": "PENDING_ENTRY",
            "action": "BUY",
            "order": {
                "order_type": "限价委托",
                "maximum_buy_price": maximum,
                "low_open_warning_price": warning,
                "valid_for": "NEXT_TRADING_SESSION",
                "instruction": f"买入{quantity}份；委托价不得高于{maximum:.3f}元；当日未成交则失效。",
            },
        }
    return {
        **common,
        "state": "PENDING_EXIT",
        "action": "SELL",
        "order": {
            "primary_order_type": "限价委托",
            "price_instruction": "预挂价格使用券商显示的当日合法价格下限，以完成卖出为优先。",
            "continuous_fallback": "连续竞价期间可改用五档即成转限价，并设置卖出保护限价。",
            "valid_for": "NEXT_TRADING_SESSION",
            "instruction": f"卖出{quantity}份；未收到明确成交回报前继续按持仓处理。",
        },
    }


def run_advice(context: RepositoryContext, request: AdviceCommand) -> CommandResult:
    """Generate advice from the latest complete locally tracked close."""
    try:
        _validate_account_input(request.actual_position, request.quantity)
    except ValueError as exc:
        raise UsageError("invalid_account_state", str(exc)) from exc
    try:
        baseline = resolve_baseline(
            context.baseline_root,
            request.baseline,
            symbol=request.symbol,
        )
        policy = resolve_execution_policy(
            context.execution_policy_root,
            symbol=request.symbol,
            baseline_version=baseline.version,
            baseline_sha256=baseline.sha256,
            required=True,
        )
        assert policy is not None
        data = load_market_data(context.raw_dir, request.symbol, request.asset_type)
        factor_result = generate_factor_frame(data)
        close = pd.Series(
            data.daily["close"].astype(float).to_numpy(),
            index=pd.DatetimeIndex(pd.to_datetime(data.daily["dt"]), name="dt"),
            name="close",
        )
        applied = apply_resolved_baseline(
            factor_result.frame,
            baseline,
            daily_close=close,
        )
        signal_date = pd.Timestamp(close.index[-1])
        target = int(applied.target_position.loc[signal_date])
        advice = build_advice(
            signal_date=signal_date,
            close=float(close.loc[signal_date]),
            target_position=target,
            actual_position=request.actual_position,
            quantity=request.quantity,
            policy=policy,
        )
        result = {
            "symbol": data.symbol,
            "data_cutoff": str(signal_date.date()),
            "baseline": {"version": baseline.version, "sha256": baseline.sha256},
            "execution_policy": {"version": policy.version, "sha256": policy.sha256},
            "factor_score": float(applied.scores.loc[signal_date]),
            **advice,
            "confirmation_rule": "未收到明确成交回报时，实际仓位保持不变。",
            "scope": "人工辅助建议；程序不连接券商、不自动下单。",
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
