"""Daily next-open execution verified by vectorbt and an independent ledger."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import vectorbt as vbt

from .objectives import RETURN_TARGETS, evaluate_return


@dataclass(frozen=True)
class BacktestResult:
    portfolio: object
    equity: pd.Series
    orders: pd.DataFrame
    metrics: dict[str, float | int]


@dataclass(frozen=True)
class PeriodBacktestResult:
    """An independently funded backtest for one requested evaluation period."""

    portfolio: object
    equity: pd.Series
    orders: pd.DataFrame
    metrics: dict[str, float | int | bool | str]
    factor_events: pd.DataFrame


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


def _readable_orders(
    portfolio: object,
    index: pd.DatetimeIndex,
    initial_signal_date: pd.Timestamp | None = None,
) -> pd.DataFrame:
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
    if initial_signal_date is not None:
        previous_dates.loc[index[0]] = initial_signal_date
    orders.insert(0, "signal_date", orders["execution_date"].map(previous_dates))
    return orders[["signal_date", "execution_date", "side", "size", "price", "fees"]].reset_index(drop=True)


def run_backtest(
    daily: pd.DataFrame,
    target_position: pd.Series,
    fee_rate: float = 0.0005,
    init_cash: float = 1_000_000.0,
    *,
    initial_target: float = 0.0,
    initial_signal_date: pd.Timestamp | None = None,
) -> BacktestResult:
    """Execute decision-date positions at the following session's open."""
    prices = _normalize_prices(daily)
    target = target_position.reindex(prices.index)
    if target.isna().any() or not target.isin([0.0, 1.0]).all():
        raise ValueError("target positions must be long/cash values 0 or 1")
    if initial_target not in (0.0, 1.0):
        raise ValueError("initial_target must be long/cash value 0 or 1")
    execution_target = target.shift(1).fillna(0.0).rename("execution_target")
    execution_target.iloc[0] = initial_target

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

    orders = _readable_orders(portfolio, prices.index, initial_signal_date)
    sharpe = float(portfolio.sharpe_ratio()) if len(prices) > 2 else float("nan")
    metrics: dict[str, float | int] = {
        "total_return": float(portfolio.total_return()),
        "max_drawdown": float(portfolio.max_drawdown()),
        "sharpe": sharpe,
        "trade_count": int(len(orders)),
        "exposure": float(execution_target.mean()),
    }
    return BacktestResult(portfolio, equity, orders, metrics)


def _factor_snapshot(factor_frame: pd.DataFrame, signal_date: pd.Timestamp) -> dict[str, object]:
    frame = factor_frame.copy()
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index), name="dt")
    if signal_date not in frame.index:
        raise AssertionError(f"factor snapshot missing for {signal_date.date()}")
    columns = (
        "structure",
        "trend",
        "volume_position",
        "factor_score",
        "enter_threshold",
        "exit_threshold",
    )
    return {column: frame.loc[signal_date, column] for column in columns if column in frame.columns}


def _attach_factor_provenance(
    period: str,
    orders: pd.DataFrame,
    factor_events: pd.DataFrame,
    factor_frame: pd.DataFrame,
    period_start: pd.Timestamp,
    initial_signal_date: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    enriched = orders.copy()
    if enriched.empty:
        enriched.insert(0, "period", pd.Series(dtype="string"))
        enriched["factor_event_id"] = pd.Series(dtype="string")
        enriched["event_type"] = pd.Series(dtype="string")
        return enriched, pd.DataFrame()
    source = factor_events.copy()
    if not source.empty:
        source["signal_date"] = pd.to_datetime(source["signal_date"])
    event_ids: list[str] = []
    event_types: list[str] = []
    used_events: list[dict[str, object]] = []
    for _, order in enriched.iterrows():
        signal_date = pd.Timestamp(order["signal_date"])
        execution_date = pd.Timestamp(order["execution_date"])
        side = str(order["side"])
        is_initial = execution_date == period_start and signal_date == initial_signal_date and side == "Buy"
        if is_initial:
            event_id = f"InitialEntry:{period}:{signal_date:%Y%m%d}"
            event_type = "InitialEntry"
            event = {
                "event_id": event_id,
                "signal_date": signal_date,
                "event_type": event_type,
                **_factor_snapshot(factor_frame, signal_date),
                "before_position": 0.0,
                "after_position": 1.0,
                "reason": "period starts in cash and aligns to prior active CZSC factor target",
            }
        else:
            expected_type = "Entry" if side == "Buy" else "Exit"
            matches = source.loc[
                (source["signal_date"] == signal_date)
                & (source["event_type"] == expected_type)
            ] if not source.empty else source
            if len(matches) != 1:
                raise AssertionError(
                    f"{period} {signal_date.date()} {side} has {len(matches)} matching factor events"
                )
            event = matches.iloc[0].to_dict()
            event_id = str(event["event_id"])
            event_type = expected_type
        event_ids.append(event_id)
        event_types.append(event_type)
        used_events.append(event)
    enriched.insert(0, "period", period)
    enriched["factor_event_id"] = event_ids
    enriched["event_type"] = event_types
    return enriched, pd.DataFrame(used_events).drop_duplicates(subset=["event_id"]).reset_index(drop=True)


def run_period_backtests(
    daily: pd.DataFrame,
    target_position: pd.Series,
    periods: dict[str, tuple[pd.Timestamp, pd.Timestamp]],
    fee_rate: float = 0.0005,
    init_cash: float = 1_000_000.0,
    *,
    factor_events: pd.DataFrame | None = None,
    factor_frame: pd.DataFrame | None = None,
    return_targets: dict[str, float] | None = None,
) -> dict[str, PeriodBacktestResult]:
    """Run independently funded portfolios over requested date ranges."""
    prices = _normalize_prices(daily)
    target = target_position.reindex(prices.index)
    effective_targets = RETURN_TARGETS if return_targets is None else return_targets
    if set(periods) != set(effective_targets):
        raise ValueError("period names must exactly match return target names")
    results: dict[str, PeriodBacktestResult] = {}
    for name, (requested_start, requested_end) in periods.items():
        period_index = prices.index[
            (prices.index >= pd.Timestamp(requested_start))
            & (prices.index <= pd.Timestamp(requested_end))
        ]
        if period_index.empty:
            raise ValueError(f"{name}: no prices in requested period")
        start, end = period_index[0], period_index[-1]
        prior_index = prices.index[prices.index < start]
        if prior_index.empty:
            raise ValueError(f"{name}: no prior signal available before period start")
        signal_date = prior_index[-1]
        period_backtest = run_backtest(
            prices.loc[start:end],
            target.loc[start:end],
            fee_rate=fee_rate,
            init_cash=init_cash,
            initial_target=float(target.loc[signal_date]),
            initial_signal_date=signal_date,
        )
        orders = period_backtest.orders
        provenance_events = pd.DataFrame()
        if factor_events is not None or factor_frame is not None:
            if factor_events is None or factor_frame is None:
                raise ValueError("factor_events and factor_frame must be provided together")
            orders, provenance_events = _attach_factor_provenance(
                name,
                orders,
                factor_events,
                factor_frame,
                start,
                signal_date,
            )
        buyhold_terminal = (
            init_cash
            / (float(prices.loc[start, "open"]) * (1.0 + fee_rate))
            * float(prices.loc[end, "close"])
        )
        strategy_return = float(period_backtest.equity.iloc[-1] / init_cash - 1.0)
        buyhold_return = float(buyhold_terminal / init_cash - 1.0)
        objective = evaluate_return(name, strategy_return, effective_targets)
        metrics: dict[str, float | int | bool | str] = {
            **period_backtest.metrics,
            "start": str(start.date()),
            "end": str(end.date()),
            "strategy_return": strategy_return,
            "buyhold_return": buyhold_return,
            "excess_return": strategy_return - buyhold_return,
            **objective,
        }
        results[name] = PeriodBacktestResult(
            period_backtest.portfolio,
            period_backtest.equity,
            orders,
            metrics,
            provenance_events,
        )
    return results
