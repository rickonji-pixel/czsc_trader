"""Observational trade diagnostics that cannot influence strategy positions."""

from __future__ import annotations

import pandas as pd


DIAGNOSTIC_COLUMNS = [
    "period",
    "entry_signal_date",
    "entry_execution_date",
    "exit_signal_date",
    "exit_execution_date",
    "holding_trading_days",
    "size",
    "entry_price",
    "exit_price",
    "entry_fees",
    "exit_fees",
    "net_return",
    "one_signal_day_exit",
    "july_august_2026",
    "entry_factor_event_id",
    "exit_factor_event_id",
]


def build_trade_diagnostics(
    orders: pd.DataFrame,
    trading_dates: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Pair completed long/cash orders and compute fee-inclusive round trips."""
    if orders.empty:
        return pd.DataFrame(columns=DIAGNOSTIC_COLUMNS)
    normalized = orders.copy()
    for column in ("signal_date", "execution_date"):
        normalized[column] = pd.to_datetime(normalized[column])
    calendar = pd.DatetimeIndex(pd.to_datetime(trading_dates)).sort_values().unique()
    calendar_positions = {pd.Timestamp(dt): index for index, dt in enumerate(calendar)}
    rows: list[dict[str, object]] = []
    for period, period_orders in normalized.groupby("period", sort=False):
        entry: pd.Series | None = None
        ordered = period_orders.sort_values("execution_date", kind="stable")
        for _, order in ordered.iterrows():
            side = str(order["side"])
            if side == "Buy":
                if entry is not None:
                    raise ValueError(f"{period}: overlapping buy orders in long/cash diagnostics")
                entry = order
                continue
            if side != "Sell":
                raise ValueError(f"{period}: unsupported order side {side}")
            if entry is None:
                raise ValueError(f"{period}: sell order has no preceding buy")
            entry_execution = pd.Timestamp(entry["execution_date"])
            exit_execution = pd.Timestamp(order["execution_date"])
            entry_signal = pd.Timestamp(entry["signal_date"])
            exit_signal = pd.Timestamp(order["signal_date"])
            size = float(entry["size"])
            entry_cost = size * float(entry["price"]) + float(entry["fees"])
            exit_proceeds = float(order["size"]) * float(order["price"]) - float(order["fees"])
            if entry_execution not in calendar_positions or exit_execution not in calendar_positions:
                raise ValueError(f"{period}: execution date missing from trading calendar")
            signal_gap = (
                calendar_positions[exit_signal] - calendar_positions[entry_signal]
                if entry_signal in calendar_positions and exit_signal in calendar_positions
                else None
            )
            july_august = (
                pd.Timestamp("2026-07-01") <= entry_signal <= pd.Timestamp("2026-08-31")
                or pd.Timestamp("2026-07-01") <= exit_signal <= pd.Timestamp("2026-08-31")
            )
            rows.append(
                {
                    "period": period,
                    "entry_signal_date": entry_signal,
                    "entry_execution_date": entry_execution,
                    "exit_signal_date": exit_signal,
                    "exit_execution_date": exit_execution,
                    "holding_trading_days": calendar_positions[exit_execution]
                    - calendar_positions[entry_execution],
                    "size": size,
                    "entry_price": float(entry["price"]),
                    "exit_price": float(order["price"]),
                    "entry_fees": float(entry["fees"]),
                    "exit_fees": float(order["fees"]),
                    "net_return": exit_proceeds / entry_cost - 1.0,
                    "one_signal_day_exit": signal_gap == 1,
                    "july_august_2026": july_august,
                    "entry_factor_event_id": entry["factor_event_id"],
                    "exit_factor_event_id": order["factor_event_id"],
                }
            )
            entry = None
    return pd.DataFrame(rows, columns=DIAGNOSTIC_COLUMNS)
