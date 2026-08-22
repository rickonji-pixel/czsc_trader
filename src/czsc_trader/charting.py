"""Interactive explanatory charts for independently funded backtest periods."""

from __future__ import annotations

from pathlib import Path

import czsc
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


DIVERGENCE_CONFIG = [
    {"name": "cxt_five_bi_V230619", "freq": "日线", "di": 1},
    {"name": "cxt_seven_bi_V230620", "freq": "日线", "di": 1},
]
FACTOR_COLUMNS = ("structure", "trend", "volume_position", "factor_score")


def extract_factor_markers(
    factors: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """Return factor-position transitions inside an inclusive date range."""
    position = factors["base_target_position"].astype(float)
    changed = position.ne(position.shift(1))
    selected = position.index[changed & position.index.to_series().between(start, end)]
    rows = [
        {
            "dt": pd.Timestamp(dt),
            "side": "FactorBuy" if position.loc[dt] == 1.0 else "FactorExit",
        }
        for dt in selected
        if dt != position.index[0]
    ]
    return pd.DataFrame(rows, columns=["dt", "side"])


def _normalize_daily(daily: pd.DataFrame) -> pd.DataFrame:
    frame = daily.copy()
    if "dt" in frame.columns:
        frame = frame.set_index("dt")
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index), name="dt")
    return frame.sort_index()


def _to_raw_bars(frame: pd.DataFrame) -> list:
    source = frame.reset_index()
    columns = ["dt", "symbol", "open", "close", "high", "low", "vol", "amount"]
    return czsc.format_standard_kline(source[columns], freq="日线")


def extract_pen_points(daily: pd.DataFrame, end: pd.Timestamp) -> pd.DataFrame:
    """Return endpoints of CZSC pens confirmed using no bar after ``end``."""
    history = _normalize_daily(daily).loc[:end]
    analyzer = czsc.CZSC(_to_raw_bars(history))
    bis = list(analyzer.bi_list)
    if not bis:
        return pd.DataFrame(columns=["dt", "price"])
    points = [{"dt": pd.Timestamp(bis[0].fx_a.dt), "price": float(bis[0].fx_a.fx)}]
    points.extend(
        {"dt": pd.Timestamp(bi.fx_b.dt), "price": float(bi.fx_b.fx)}
        for bi in bis
    )
    return pd.DataFrame(points)


def extract_divergence_markers(
    daily: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """Return causal five/seven-pen CZSC divergence transitions in a period."""
    history = _normalize_daily(daily).loc[:end]
    bars = _to_raw_bars(history)
    rows: list[dict[str, object]] = []
    for config in DIVERGENCE_CONFIG:
        output = czsc.generate_czsc_signals(
            bars,
            [config],
            sdt=str(history.index.min().date()),
            init_n=30,
            df=True,
        )
        signal_columns = [column for column in output.columns if len(column.split("_")) == 3]
        if len(signal_columns) != 1:
            raise ValueError(f"Unexpected CZSC divergence columns for {config}: {signal_columns}")
        signal = output[signal_columns[0]].astype("string")
        transitions = signal.ne(signal.shift(1)).fillna(signal.notna())
        for row_index in output.index[transitions & signal.str.contains("背驰", na=False)]:
            dt = pd.Timestamp(output.loc[row_index, "dt"])
            if dt.tzinfo is not None:
                dt = dt.tz_localize(None)
            if not start <= dt <= end:
                continue
            label = str(signal.loc[row_index]).split("_", 1)[0]
            side = "Bottom" if "底背驰" in label else "Top" if "顶背驰" in label else None
            if side is not None:
                rows.append(
                    {
                        "dt": dt,
                        "side": side,
                        "label": label,
                        "source": str(config["name"]),
                    }
                )
    return pd.DataFrame(rows, columns=["dt", "side", "label", "source"])


def _price_for_dates(prices: pd.DataFrame, dates: pd.Series, column: str) -> list[float]:
    return [float(prices.loc[pd.Timestamp(dt), column]) for dt in dates]


def _add_empty_or_marker(
    figure: go.Figure,
    frame: pd.DataFrame,
    *,
    name: str,
    side: str,
    prices: pd.DataFrame,
    price_column: str,
    color: str,
    symbol: str,
    text_column: str | None = None,
) -> None:
    selected = frame.loc[frame["side"] == side] if not frame.empty else frame
    dates = selected["dt"] if "dt" in selected else pd.Series(dtype="datetime64[ns]")
    text = selected[text_column].astype(str) if text_column and text_column in selected else None
    figure.add_trace(
        go.Scatter(
            x=dates,
            y=_price_for_dates(prices, dates, price_column) if len(dates) else [],
            mode="markers",
            name=name,
            text=text,
            marker={"color": color, "symbol": symbol, "size": 11, "line": {"width": 1, "color": "#222"}},
            hovertemplate=("%{x|%Y-%m-%d}<br>%{text}<extra>" + name + "</extra>") if text is not None else None,
        ),
        row=1,
        col=1,
    )


def build_period_chart(
    daily: pd.DataFrame,
    factors: pd.DataFrame,
    orders: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    title: str,
) -> go.Figure:
    """Build a two-panel daily chart for one independently funded period."""
    prices = _normalize_daily(daily)
    period = prices.loc[start:end]
    if period.empty:
        raise ValueError("No daily bars in chart period")
    aligned_factors = factors.copy()
    aligned_factors.index = pd.DatetimeIndex(pd.to_datetime(aligned_factors.index), name="dt")
    aligned_factors = aligned_factors.sort_index()

    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.72, 0.28],
    )
    figure.add_trace(
        go.Candlestick(
            x=period.index,
            open=period["open"],
            high=period["high"],
            low=period["low"],
            close=period["close"],
            name="日K",
            increasing_line_color="#d62728",
            decreasing_line_color="#2ca02c",
        ),
        row=1,
        col=1,
    )

    pen_points = extract_pen_points(prices, end)
    pen_points = pen_points.loc[pen_points["dt"].between(start, end)] if not pen_points.empty else pen_points
    figure.add_trace(
        go.Scatter(
            x=pen_points.get("dt", []),
            y=pen_points.get("price", []),
            mode="lines+markers",
            name="CZSC笔",
            line={"color": "#1f77b4", "width": 2},
            marker={"size": 5},
            hovertemplate="%{x|%Y-%m-%d}<br>笔端点 %{y:.3f}<extra>CZSC笔</extra>",
        ),
        row=1,
        col=1,
    )

    divergences = extract_divergence_markers(prices, start, end)
    _add_empty_or_marker(
        figure,
        divergences,
        name="底背驰",
        side="Bottom",
        prices=prices,
        price_column="low",
        color="#9467bd",
        symbol="diamond",
        text_column="label",
    )
    _add_empty_or_marker(
        figure,
        divergences,
        name="顶背驰",
        side="Top",
        prices=prices,
        price_column="high",
        color="#8c564b",
        symbol="diamond-open",
        text_column="label",
    )

    factor_markers = extract_factor_markers(aligned_factors, start, end)
    _add_empty_or_marker(
        figure,
        factor_markers,
        name="因子入场",
        side="FactorBuy",
        prices=prices,
        price_column="low",
        color="#17becf",
        symbol="triangle-up",
    )
    _add_empty_or_marker(
        figure,
        factor_markers,
        name="因子离场",
        side="FactorExit",
        prices=prices,
        price_column="high",
        color="#ff7f0e",
        symbol="triangle-down",
    )

    actual_orders = orders.copy()
    if not actual_orders.empty:
        actual_orders["dt"] = pd.to_datetime(actual_orders["execution_date"])
        actual_orders = actual_orders.loc[actual_orders["dt"].between(start, end)]
    for side, name, color, symbol in (
        ("Buy", "策略买入", "#d62728", "triangle-up"),
        ("Sell", "策略卖出", "#2ca02c", "triangle-down"),
    ):
        selected = actual_orders.loc[actual_orders["side"] == side] if not actual_orders.empty else actual_orders
        figure.add_trace(
            go.Scatter(
                x=selected.get("dt", []),
                y=selected.get("price", []),
                mode="markers",
                name=name,
                customdata=(selected[["signal_date", "size", "fees"]].to_numpy() if not selected.empty else []),
                marker={"color": color, "symbol": symbol, "size": 15, "line": {"width": 2, "color": "#111"}},
                hovertemplate=(
                    "%{x|%Y-%m-%d}<br>成交价 %{y:.3f}<br>信号日 %{customdata[0]}"
                    "<br>份额 %{customdata[1]:,.0f}<br>费用 %{customdata[2]:,.2f}<extra>" + name + "</extra>"
                ),
            ),
            row=1,
            col=1,
        )

    colors = {
        "structure": "#1f77b4",
        "trend": "#ff7f0e",
        "volume_position": "#2ca02c",
        "factor_score": "#d62728",
    }
    factor_period = aligned_factors.loc[start:end]
    for column in FACTOR_COLUMNS:
        figure.add_trace(
            go.Scatter(
                x=factor_period.index,
                y=factor_period[column],
                mode="lines",
                name=column,
                line={"color": colors[column], "width": 2 if column == "factor_score" else 1},
                hovertemplate=f"%{{x|%Y-%m-%d}}<br>{column} %{{y:.3f}}<extra>{column}</extra>",
            ),
            row=2,
            col=1,
        )

    figure.add_hline(y=0.0, line_width=1, line_dash="dot", line_color="#777", row=2, col=1)
    figure.update_layout(
        title=title,
        height=900,
        template="plotly_white",
        hovermode="x unified",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "left", "x": 0},
        margin={"l": 60, "r": 30, "t": 100, "b": 50},
        xaxis_rangeslider_visible=False,
    )
    figure.update_xaxes(rangebreaks=[{"bounds": ["sat", "mon"]}])
    figure.update_yaxes(title_text="价格", row=1, col=1)
    figure.update_yaxes(title_text="CZSC因子", range=[-1.1, 1.1], row=2, col=1)
    return figure


def write_period_chart(
    daily: pd.DataFrame,
    factors: pd.DataFrame,
    orders: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    title: str,
    output_path: Path,
) -> Path:
    """Write a self-contained interactive period chart and return its path."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure = build_period_chart(daily, factors, orders, start, end, title)
    figure.write_html(output_path, include_plotlyjs=True, full_html=True)
    return output_path
