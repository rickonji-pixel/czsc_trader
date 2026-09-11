"""Causal low-attention limit-order execution policies."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

import numpy as np
import pandas as pd

from .strategy_metrics import strategy_comparison_metrics


@dataclass(frozen=True)
class ExecutionSimulation:
    equity: pd.Series
    orders: pd.DataFrame
    daily_state: pd.DataFrame
    cycles: pd.DataFrame
    metrics: dict[str, float | int | None]


def floor_to_tick(price: float, tick: float = 0.001) -> float:
    """Round a positive price down without exceeding the theoretical cap."""
    price_value = Decimal(str(price))
    tick_value = Decimal(str(tick))
    if not price_value.is_finite() or price_value <= 0:
        raise ValueError("price must be positive and finite")
    if not tick_value.is_finite() or tick_value <= 0:
        raise ValueError("tick must be positive and finite")
    ticks = (price_value / tick_value).to_integral_value(rounding=ROUND_FLOOR)
    return float(ticks * tick_value)


def ceil_to_tick(price: float, tick: float = 0.001) -> float:
    """Round a positive price up without crossing below a theoretical floor."""
    price_value = Decimal(str(price))
    tick_value = Decimal(str(tick))
    if not price_value.is_finite() or price_value <= 0:
        raise ValueError("price must be positive and finite")
    if not tick_value.is_finite() or tick_value <= 0:
        raise ValueError("tick must be positive and finite")
    ticks = (price_value / tick_value).to_integral_value(rounding=ROUND_CEILING)
    return float(ticks * tick_value)


def round_to_tick(price: float, tick: float = 0.001) -> float:
    """Round a positive price to the nearest tick with half values rounded up."""
    price_value = Decimal(str(price))
    tick_value = Decimal(str(tick))
    if not price_value.is_finite() or price_value <= 0:
        raise ValueError("price must be positive and finite")
    if not tick_value.is_finite() or tick_value <= 0:
        raise ValueError("tick must be positive and finite")
    ticks = (price_value / tick_value).to_integral_value(rounding=ROUND_HALF_UP)
    return float(ticks * tick_value)


def _daily_prices(daily: pd.DataFrame) -> pd.DataFrame:
    frame = daily.copy()
    if "dt" in frame.columns:
        frame = frame.set_index("dt")
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index), name="dt")
    frame = frame.sort_index()
    required = {"open", "high", "low", "close"}
    if not required <= set(frame.columns):
        raise ValueError(f"daily prices missing columns: {sorted(required - set(frame.columns))}")
    values = frame.loc[:, ["open", "high", "low", "close"]].astype(float)
    if values.empty or not np.isfinite(values.to_numpy()).all() or (values <= 0).any().any():
        raise ValueError("daily OHLC must be positive and finite")
    if (values["high"] < values[["open", "close"]].max(axis=1)).any():
        raise ValueError("daily high is below open or close")
    if (values["low"] > values[["open", "close"]].min(axis=1)).any():
        raise ValueError("daily low is above open or close")
    return frame


def average_true_range(daily: pd.DataFrame, window: int = 20) -> pd.Series:
    """Return a causal simple moving average of daily True Range."""
    if int(window) <= 0:
        raise ValueError("ATR window must be positive")
    frame = _daily_prices(daily)
    previous_close = frame["close"].astype(float).shift(1)
    true_range = pd.concat(
        [
            frame["high"].astype(float).sub(frame["low"].astype(float)),
            frame["high"].astype(float).sub(previous_close).abs(),
            frame["low"].astype(float).sub(previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(int(window), min_periods=int(window)).mean().rename("atr")


def entry_limit_series(
    daily: pd.DataFrame,
    family: str,
    parameter: float,
    atr_window: int = 20,
    tick: float = 0.001,
) -> pd.Series:
    """Return signal-date entry caps for one fixed or ATR candidate."""
    frame = _daily_prices(daily)
    value = float(parameter)
    if not np.isfinite(value):
        raise ValueError("entry-limit parameter must be finite")
    if family == "fixed":
        raw = frame["close"].astype(float).mul(1.0 + value)
    elif family == "atr":
        raw = frame["close"].astype(float).add(average_true_range(frame, atr_window).mul(value))
    else:
        raise ValueError(f"unknown entry-limit family: {family}")
    return raw.map(lambda price: floor_to_tick(price, tick) if pd.notna(price) else np.nan).rename(
        "entry_limit"
    )


def _intraday_prices(intraday: pd.DataFrame) -> pd.DataFrame:
    frame = intraday.copy()
    if "dt" in frame.columns:
        frame = frame.set_index("dt")
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index), name="dt")
    frame = frame.sort_index()
    required = {"low", "vol"}
    if not required <= set(frame.columns):
        raise ValueError(f"intraday prices missing columns: {sorted(required - set(frame.columns))}")
    values = frame.loc[:, ["low", "vol"]].astype(float)
    if values.empty or not np.isfinite(values.to_numpy()).all():
        raise ValueError("intraday low and volume must be finite and non-empty")
    if (values["low"] <= 0).any() or (values["vol"] < 0).any():
        raise ValueError("intraday low must be positive and volume non-negative")
    return frame


def _empty_orders() -> pd.DataFrame:
    return pd.DataFrame(
        columns=(
            "signal_date",
            "execution_date",
            "side",
            "size",
            "price",
            "fees",
            "trigger",
            "entry_limit",
            "cap_premium",
            "open_improvement",
            "touch_volume",
            "diagnostic_participation",
        )
    )


def simulate_limit_policy(
    daily: pd.DataFrame,
    intraday: pd.DataFrame,
    target_position: pd.Series,
    entry_limits: pd.Series,
    fee_rate: float = 0.0005,
    init_cash: float = 1_000_000.0,
    diagnostic_quantity: int = 50_000,
    lot_size: int | None = None,
    fill_on_equal_touch: bool = True,
    slippage_bp: int = 0,
) -> ExecutionSimulation:
    """Simulate pre-open daily entry limits and priority next-open exits."""
    prices = _daily_prices(daily)
    bars = _intraday_prices(intraday)
    target = target_position.astype(float).copy()
    target.index = pd.DatetimeIndex(pd.to_datetime(target.index), name="dt")
    target = target.reindex(prices.index)
    limits = entry_limits.astype(float).copy()
    limits.index = pd.DatetimeIndex(pd.to_datetime(limits.index), name="dt")
    limits = limits.reindex(prices.index)
    if target.isna().any() or not target.isin((0.0, 1.0)).all():
        raise ValueError("target positions must align and contain only 0 or 1")
    if not np.isfinite(float(fee_rate)) or not 0 <= float(fee_rate) < 1:
        raise ValueError("fee rate must be finite and in [0, 1)")
    if not np.isfinite(float(init_cash)) or float(init_cash) <= 0:
        raise ValueError("initial cash must be positive and finite")
    if int(diagnostic_quantity) <= 0:
        raise ValueError("diagnostic quantity must be positive")
    if lot_size is not None and int(lot_size) <= 0:
        raise ValueError("lot size must be positive")
    if int(slippage_bp) < 0:
        raise ValueError("slippage_bp must be non-negative")
    slippage = float(slippage_bp) / 10_000.0

    cash = float(init_cash)
    shares = 0.0
    actual_position = 0.0
    order_rows: list[dict[str, object]] = []
    state_rows: list[dict[str, object]] = []
    cycle_rows: list[dict[str, object]] = []
    active_cycle: dict[str, object] | None = None
    attempt_count = 0

    for location, (date, row) in enumerate(prices.iterrows()):
        signal_date = prices.index[location - 1] if location > 0 else pd.NaT
        desired = float(target.loc[signal_date]) if location > 0 else 0.0
        previous_desired = (
            float(target.loc[prices.index[location - 2]]) if location > 1 else 0.0
        )
        entry_limit = float(limits.loc[signal_date]) if location > 0 and pd.notna(limits.loc[signal_date]) else np.nan
        action = "hold"

        if desired == 1.0 and previous_desired == 0.0:
            active_cycle = {
                "signal_date": signal_date,
                "first_execution_date": date,
                "filled": False,
                "t1_filled": False,
                "fill_date": pd.NaT,
                "fill_price": np.nan,
                "wait_sessions": 0,
            }
            attempt_count = 0

        if desired == 1.0 and actual_position == 0.0 and np.isfinite(entry_limit):
            attempt_count += 1
            if active_cycle is not None:
                active_cycle["wait_sessions"] = attempt_count
            open_price = float(row["open"])
            fill_timestamp: pd.Timestamp | None = None
            fill_price: float | None = None
            trigger: str | None = None
            touch_volume: float | None = None
            day_bars = bars.loc[bars.index.normalize() == date.normalize()]
            if open_price <= entry_limit:
                fill_timestamp = date
                fill_price = open_price
                trigger = "open"
                if not day_bars.empty:
                    touch_volume = float(day_bars.iloc[0]["vol"])
            elif (
                float(row["low"]) <= entry_limit
                if fill_on_equal_touch
                else float(row["low"]) < entry_limit
            ):
                lows = day_bars["low"].astype(float)
                touches = day_bars.loc[
                    lows.le(entry_limit) if fill_on_equal_touch else lows.lt(entry_limit)
                ]
                if touches.empty:
                    raise AssertionError(f"daily low touches limit but 30m bars do not: {date.date()}")
                fill_timestamp = pd.Timestamp(touches.index[0])
                fill_price = entry_limit
                trigger = "intraday_limit"
                touch_volume = float(touches.iloc[0]["vol"])
            if fill_price is not None and fill_timestamp is not None and trigger is not None:
                fill_price *= 1.0 + slippage
                affordable = cash / (fill_price * (1.0 + float(fee_rate)))
                shares = (
                    affordable
                    if lot_size is None
                    else float(int(affordable // int(lot_size)) * int(lot_size))
                )
                if shares <= 0:
                    action = "entry_unfilled"
                else:
                    fees = shares * fill_price * float(fee_rate)
                    cash -= shares * fill_price + fees
                    if abs(cash) < 1e-8:
                        cash = 0.0
                    actual_position = 1.0
                    action = "buy"
                    participation = (
                        float(diagnostic_quantity) / touch_volume
                        if touch_volume is not None and touch_volume > 0
                        else np.nan
                    )
                    order_rows.append({
                        "signal_date": signal_date,
                        "execution_date": fill_timestamp,
                        "side": "Buy",
                        "size": shares,
                        "price": fill_price,
                        "fees": fees,
                        "trigger": trigger,
                        "entry_limit": entry_limit,
                        "cap_premium": entry_limit / float(prices.loc[signal_date, "close"]) - 1.0,
                        "open_improvement": open_price / fill_price - 1.0,
                        "touch_volume": touch_volume,
                        "diagnostic_participation": participation,
                    })
                    if active_cycle is not None:
                        active_cycle.update({
                            "filled": True,
                            "t1_filled": attempt_count == 1,
                            "fill_date": fill_timestamp,
                            "fill_price": fill_price,
                        })
            else:
                action = "entry_unfilled"

        if desired == 0.0 and actual_position == 1.0:
            exit_price = float(row["open"]) * (1.0 - slippage)
            proceeds = shares * exit_price
            fees = proceeds * float(fee_rate)
            cash += proceeds - fees
            order_rows.append(
                {
                    "signal_date": signal_date,
                    "execution_date": date,
                    "side": "Sell",
                    "size": shares,
                    "price": exit_price,
                    "fees": fees,
                    "trigger": "priority_open_exit",
                    "entry_limit": np.nan,
                    "cap_premium": np.nan,
                    "open_improvement": np.nan,
                    "touch_volume": np.nan,
                    "diagnostic_participation": np.nan,
                }
            )
            shares = 0.0
            actual_position = 0.0
            action = "sell"

        if desired == 0.0 and previous_desired == 1.0 and active_cycle is not None:
            active_cycle["cycle_end_signal_date"] = signal_date
            cycle_rows.append(active_cycle)
            active_cycle = None
            attempt_count = 0

        equity = cash + shares * float(row["close"])
        state_rows.append(
            {
                "dt": date,
                "signal_date": signal_date,
                "desired_position": desired,
                "actual_position": actual_position,
                "entry_limit": entry_limit,
                "cap_premium": (
                    entry_limit / float(prices.loc[signal_date, "close"]) - 1.0
                    if location > 0 and np.isfinite(entry_limit)
                    else np.nan
                ),
                "action": action,
                "cash": cash,
                "shares": shares,
                "equity": equity,
            }
        )

    if active_cycle is not None:
        active_cycle["cycle_end_signal_date"] = pd.NaT
        cycle_rows.append(active_cycle)

    orders = pd.DataFrame(order_rows) if order_rows else _empty_orders()
    state = pd.DataFrame(state_rows).set_index("dt")
    cycles = pd.DataFrame(cycle_rows)
    equity = state["equity"].astype(float).rename("equity")
    return ExecutionSimulation(equity, orders, state, cycles, {})


def policy_metrics(
    simulation: ExecutionSimulation,
    init_cash: float,
) -> dict[str, float | int | None]:
    """Return execution-service and portfolio metrics for one simulation."""
    equity = simulation.equity.astype(float)
    comparison = strategy_comparison_metrics(equity, simulation.orders, init_cash)
    cycles = simulation.cycles
    cycle_count = int(len(cycles))
    if cycle_count:
        t1_fill_rate = float(cycles["t1_filled"].astype(bool).mean())
        final_fill_rate = float(cycles["filled"].astype(bool).mean())
        two_day_fill_rate = float(
            (cycles["filled"].astype(bool) & cycles["wait_sessions"].astype(int).le(2)).mean()
        )
        filled_waits = cycles.loc[cycles["filled"].astype(bool), "wait_sessions"].astype(float)
        average_wait = float(filled_waits.mean()) if not filled_waits.empty else None
        longest_wait = int(filled_waits.max()) if not filled_waits.empty else None
        missed_cycles = int((~cycles["filled"].astype(bool)).sum())
    else:
        t1_fill_rate = 0.0
        two_day_fill_rate = 0.0
        final_fill_rate = 0.0
        average_wait = None
        longest_wait = None
        missed_cycles = 0
    caps = simulation.daily_state.loc[
        simulation.daily_state["desired_position"].eq(1.0)
        & simulation.daily_state["actual_position"].shift(1, fill_value=0.0).eq(0.0),
        "cap_premium",
    ].dropna().astype(float)
    buys = simulation.orders.loc[simulation.orders["side"].eq("Buy")]
    participation = buys["diagnostic_participation"].dropna().astype(float)
    improvements = buys["open_improvement"].dropna().astype(float)
    return {
        **comparison,
        "cycle_count": cycle_count,
        "t1_fill_rate": t1_fill_rate,
        "two_day_fill_rate": two_day_fill_rate,
        "final_fill_rate": final_fill_rate,
        "average_wait_sessions": average_wait,
        "longest_wait_sessions": longest_wait,
        "missed_cycles": missed_cycles,
        "cap_p95": float(caps.quantile(0.95)) if not caps.empty else None,
        "cap_mean": float(caps.mean()) if not caps.empty else None,
        "average_open_improvement": float(improvements.mean()) if not improvements.empty else None,
        "worst_open_improvement": float(improvements.min()) if not improvements.empty else None,
        "average_diagnostic_participation": (
            float(participation.mean()) if not participation.empty else None
        ),
        "maximum_diagnostic_participation": (
            float(participation.max()) if not participation.empty else None
        ),
        "trade_count": int(len(simulation.orders)),
    }


def select_execution_candidate(
    candidates: pd.DataFrame,
    minimum_t1_fill_rate: float = 0.90,
) -> pd.Series:
    """Select the cheapest deterministic candidate meeting the fill service."""
    threshold = float(minimum_t1_fill_rate)
    if not np.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise ValueError("minimum T+1 fill rate must be in [0, 1]")
    required = {
        "candidate_id",
        "t1_fill_rate",
        "cap_p95",
        "cap_mean",
        "worst_calmar",
        "worst_drawdown",
        "family_rank",
        "parameter",
    }
    if not required <= set(candidates.columns):
        raise ValueError(f"candidate metrics missing columns: {sorted(required - set(candidates.columns))}")
    eligible = candidates.loc[candidates["t1_fill_rate"].astype(float).ge(threshold)].copy()
    if eligible.empty:
        raise ValueError("no execution candidate meets the T+1 fill service level")
    ranked = eligible.sort_values(
        [
            "cap_p95",
            "cap_mean",
            "worst_calmar",
            "worst_drawdown",
            "family_rank",
            "parameter",
            "candidate_id",
        ],
        ascending=[True, True, False, False, True, True, True],
        na_position="last",
        kind="stable",
    )
    return ranked.iloc[0].copy()
