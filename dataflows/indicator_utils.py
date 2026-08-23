from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
from stockstats import wrap

INDICATOR_DESCRIPTIONS = {
    "close_50_sma": "50 SMA: A medium-term trend indicator used to identify trend direction and dynamic support or resistance.",
    "close_200_sma": "200 SMA: A long-term trend benchmark often used for strategic trend confirmation and golden/death cross setups.",
    "close_10_ema": "10 EMA: A responsive short-term moving average used to capture quick momentum shifts.",
    "macd": "MACD: Measures momentum via differences between EMAs and is commonly used for crossover and divergence analysis.",
    "macds": "MACD Signal: Smoothed MACD line used together with MACD crossovers to detect momentum changes.",
    "macdh": "MACD Histogram: Visualizes the gap between MACD and its signal line to show momentum strength.",
    "rsi": "RSI: Measures momentum and highlights overbought or oversold conditions.",
    "boll": "Bollinger Middle: The middle moving average used as the basis for Bollinger Bands.",
    "boll_ub": "Bollinger Upper Band: Upper volatility band used to identify breakout and overbought zones.",
    "boll_lb": "Bollinger Lower Band: Lower volatility band used to identify oversold zones.",
    "atr": "ATR: Average True Range, used to measure market volatility and help size risk.",
    "vwma": "VWMA: Volume-weighted moving average that combines price trend and trading volume.",
    "mfi": "MFI: Money Flow Index, a volume-aware momentum indicator for buying and selling pressure.",
}


def compute_indicator_report(
    dataframe: pd.DataFrame,
    indicator: str,
    curr_date: str,
    look_back_days: int,
) -> str:
    if indicator not in INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: {list(INDICATOR_DESCRIPTIONS.keys())}"
        )
    if dataframe is None or dataframe.empty:
        return f"No OHLCV data available to calculate indicator `{indicator}`."

    frame = dataframe.copy()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    frame = frame.dropna(subset=["Date"]).sort_values("Date")
    wrapped = wrap(frame.rename(columns={"Date": "date"}).rename(columns=str.lower))
    wrapped[indicator]
    wrapped.index = pd.to_datetime(wrapped.index, errors="coerce")
    rows = wrapped.reset_index()
    rows["date_str"] = pd.to_datetime(rows["date"], errors="coerce").dt.strftime("%Y-%m-%d")

    values = {
        row["date_str"]: ("N/A" if pd.isna(row[indicator]) else str(row[indicator]))
        for _, row in rows.iterrows()
    }

    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_dt = curr_dt - timedelta(days=look_back_days)
    lines: list[str] = []
    pointer = curr_dt
    while pointer >= start_dt:
        key = pointer.strftime("%Y-%m-%d")
        value = values.get(key, "N/A: Not a trading day (weekend or holiday)")
        lines.append(f"{key}: {value}")
        pointer -= timedelta(days=1)

    return (
        f"## {indicator} values from {start_dt.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
        + "\n".join(lines)
        + "\n\n"
        + INDICATOR_DESCRIPTIONS[indicator]
    )
