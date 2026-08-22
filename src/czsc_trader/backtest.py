"""Daily next-open execution verified by vectorbt and an independent ledger."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import vectorbt as vbt


WINDOW_START = pd.Timestamp("2025-12-31")
WINDOW_ENDS = {
    "2026Q1": pd.Timestamp("2026-03-31"),
    "2026H1": pd.Timestamp("2026-06-30"),
    "2026_01_08": pd.Timestamp("2026-08-21"),
}


@dataclass(frozen=True)
class BacktestResult:
    portfolio: object
    equity: pd.Series
    orders: pd.DataFrame
    metrics: dict[str, float | int]
    windows: dict[str, dict[str, float | bool | str]]


def _normalize_prices(daily: pd.DataFrame) -> pd.DataFrame:
    prices = daily.copy()
    if "dt" in prices.columns:
        prices = prices.set_index("dt")
    prices.index = pd.DatetimeIndex(pd.to_datetime(prices.index), name="dt")
    prices = prices.sort_index()
    required = {"open", "close"}
    if not required <= set(prices.columns):
        raise ValueError(f"daily prices missing columns: {sorted(required - set(prices.columns))}")
    return prices


def _independent_equity(
    prices: pd.DataFrame,
    execution_target: pd.Series,
    fee_rate: float,
    init_cash: float,
) -> pd.Series:
    cash = float(init_cash)
    shares = 0.0
    previous_target = 0.0
    values: list[float] = []
    for date, row in prices.iterrows():
        desired = float(execution_target.loc[date])
        open_price = float(row["open"])
        if desired != previous_target:
            if desired == 1.0:
                shares = cash / (open_price * (1.0 + fee_rate))
                cash = 0.0
            else:
                cash = shares * open_price * (1.0 - fee_rate)
                shares = 0.0
            previous_target = desired
        values.append(cash + shares * float(row["close"]))
    return pd.Series(values, index=prices.index, name="independent_equity")


def _readable_orders(portfolio: object, index: pd.DatetimeIndex) -> pd.DataFrame:
    readable = portfolio.orders.records_readable.copy()
    if readable.empty:
        return pd.DataFrame(columns=["signal_date", "execution_date", "side", "size", "price", "fees"])
    column_map = {
        "Timestamp": "execution_date",
        "Side": "side",
        "Size": "size",
        "Price": "price",
        "Fees": "fees",
    }
    orders = readable.rename(columns=column_map)
    orders["execution_date"] = pd.to_datetime(orders["execution_date"])
    previous_dates = pd.Series(index[:-1], index=index[1:])
    orders.insert(0, "signal_date", orders["execution_date"].map(previous_dates))
    return orders[["signal_date", "execution_date", "side", "size", "price", "fees"]].reset_index(drop=True)


def _window_metrics(
    prices: pd.DataFrame,
    equity: pd.Series,
) -> dict[str, dict[str, float | bool | str]]:
    results: dict[str, dict[str, float | bool | str]] = {}
    if WINDOW_START not in prices.index:
        return results
    for name, requested_end in WINDOW_ENDS.items():
        eligible = prices.index[(prices.index >= WINDOW_START) & (prices.index <= requested_end)]
        if len(eligible) < 2:
            continue
        end = eligible[-1]
        strategy_return = float(equity.loc[end] / equity.loc[WINDOW_START] - 1.0)
        buyhold_return = float(prices.loc[end, "close"] / prices.loc[WINDOW_START, "close"] - 1.0)
        excess = strategy_return - buyhold_return
        results[name] = {
            "start": str(WINDOW_START.date()),
            "end": str(end.date()),
            "strategy_return": strategy_return,
            "buyhold_return": buyhold_return,
            "excess_return": excess,
            "pass": bool(excess > 0.0),
        }
    return results


def run_backtest(
    daily: pd.DataFrame,
    target_position: pd.Series,
    fee_rate: float = 0.0005,
    init_cash: float = 1_000_000.0,
) -> BacktestResult:
    """Execute decision-date positions at the following session's open."""
    prices = _normalize_prices(daily)
    target = target_position.reindex(prices.index)
    if target.isna().any() or not target.isin([0.0, 1.0]).all():
        raise ValueError("target positions must be long/cash values 0 or 1")
    execution_target = target.shift(1).fillna(0.0).rename("execution_target")

    portfolio = vbt.Portfolio.from_orders(
        close=prices["close"],
        size=execution_target,
        size_type="targetpercent",
        price=prices["open"],
        fees=fee_rate,
        init_cash=init_cash,
        direction="longonly",
        freq="1D",
    )
    equity = portfolio.value().rename("equity")
    independent = _independent_equity(prices, execution_target, fee_rate, init_cash)
    if not np.allclose(equity.to_numpy(), independent.to_numpy(), rtol=1e-8, atol=1e-6):
        largest = float(np.max(np.abs(equity.to_numpy() - independent.to_numpy())))
        raise AssertionError(f"vectorbt and independent equity differ; max absolute delta={largest}")

    orders = _readable_orders(portfolio, prices.index)
    sharpe = float(portfolio.sharpe_ratio()) if len(prices) > 2 else float("nan")
    metrics: dict[str, float | int] = {
        "total_return": float(portfolio.total_return()),
        "max_drawdown": float(portfolio.max_drawdown()),
        "sharpe": sharpe,
        "trade_count": int(len(orders)),
        "exposure": float(execution_target.mean()),
    }
    return BacktestResult(portfolio, equity, orders, metrics, _window_metrics(prices, equity))
