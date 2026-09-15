"""Standalone daily chart for the MA5/MA20 comparison strategy."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go


def _normalize_daily(daily: pd.DataFrame) -> pd.DataFrame:
    prices = daily.copy()
    if "dt" in prices:
        prices = prices.set_index("dt")
    prices.index = pd.DatetimeIndex(pd.to_datetime(prices.index), name="dt")
    return prices.sort_index()


def _missing_calendar_dates(index: pd.DatetimeIndex) -> list[str]:
    calendar = pd.date_range(index.min(), index.max(), freq="D")
    missing = calendar.difference(index.normalize())
    return [value.strftime("%Y-%m-%d") for value in missing]


def build_ma_chart(
    daily: pd.DataFrame,
    signals: pd.DataFrame,
    orders: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    title: str,
) -> go.Figure:
    """Build a one-panel K-line chart with MA lines and execution markers."""
    prices = _normalize_daily(daily).loc[start:end]
    if prices.empty:
        raise ValueError("No daily bars in MA chart period")
    aligned = signals.copy()
    aligned.index = pd.DatetimeIndex(pd.to_datetime(aligned.index), name="dt")
    aligned = aligned.sort_index().loc[start:end]
    span = float(prices["high"].max() - prices["low"].min())
    marker_gap = max(span * 0.025, float(prices["close"].median()) * 0.0025)

    figure = go.Figure()
    figure.add_trace(
        go.Candlestick(
            x=prices.index,
            open=prices["open"],
            high=prices["high"],
            low=prices["low"],
            close=prices["close"],
            name="日K",
            increasing_line_color="#d62728",
            decreasing_line_color="#2ca02c",
        )
    )
    for column, color in (("ma5", "#d62728"), ("ma20", "#1f77b4")):
        figure.add_trace(
            go.Scatter(
                x=aligned.index,
                y=aligned[column],
                mode="lines",
                name=column.upper(),
                line={"color": color, "width": 1.8},
                hovertemplate=f"%{{x|%Y-%m-%d}}<br>{column.upper()} %{{y:.4f}}<extra>{column.upper()}</extra>",
            )
        )

    actual_orders = orders.copy()
    if not actual_orders.empty:
        actual_orders["dt"] = pd.to_datetime(actual_orders["execution_date"])
        actual_orders = actual_orders.loc[actual_orders["dt"].between(start, end)]
    for side, name, color, symbol, price_column, offset in (
        ("Buy", "MA买入", "#d62728", "triangle-up", "low", -marker_gap),
        ("Sell", "MA卖出", "#2ca02c", "triangle-down", "high", marker_gap),
    ):
        selected = actual_orders.loc[actual_orders["side"] == side] if not actual_orders.empty else actual_orders
        dates = selected.get("dt", [])
        reference = prices.reindex(pd.DatetimeIndex(dates))[price_column].to_numpy() if len(dates) else []
        figure.add_trace(
            go.Scatter(
                x=dates,
                y=[price + offset for price in reference],
                mode="markers",
                name=name,
                customdata=(
                    selected[["signal_date", "size", "fees", "price"]].to_numpy()
                    if not selected.empty
                    else []
                ),
                marker={"color": color, "symbol": symbol, "size": 13, "line": {"width": 1, "color": "#222"}},
                hovertemplate=(
                    "%{x|%Y-%m-%d}<br>成交价 %{customdata[3]:.3f}<br>信号日 %{customdata[0]}"
                    "<br>份额 %{customdata[1]:,.0f}<br>费用 %{customdata[2]:,.2f}<extra>" + name + "</extra>"
                ),
            )
        )
    figure.update_layout(
        title=title,
        height=720,
        template="plotly_white",
        hovermode="x unified",
        hoverlabel={"bgcolor": "rgba(255, 255, 255, 0.5)"},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "left", "x": 0},
        margin={"l": 60, "r": 30, "t": 100, "b": 50},
        xaxis_rangeslider_visible=False,
    )
    figure.update_xaxes(
        rangebreaks=[{"values": _missing_calendar_dates(prices.index), "dvalue": 24 * 60 * 60 * 1000}]
    )
    figure.update_yaxes(title_text="价格")
    return figure


def write_ma_chart(
    daily: pd.DataFrame,
    signals: pd.DataFrame,
    orders: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    title: str,
    output_path: Path,
) -> Path:
    """Write a self-contained MA strategy chart."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    build_ma_chart(daily, signals, orders, start, end, title).write_html(
        output_path,
        include_plotlyjs=True,
        full_html=True,
    )
    return output_path
