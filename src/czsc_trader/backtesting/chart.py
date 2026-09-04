from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from czsc_trader.charting import (
    _missing_calendar_dates,
    _normalize_daily,
    extract_pen_points,
)

from .datasets import ReplayData
from .result import BacktestResult
from .signal_replay import SignalReplay


def _fill_markers(
    figure: go.Figure,
    fills: pd.DataFrame,
    prices: pd.DataFrame,
) -> None:
    if fills.empty:
        dated = fills.copy()
    else:
        dated = fills.copy()
        dated["date"] = pd.to_datetime(dated["fill_time"]).dt.normalize()
        dated = dated.loc[dated["date"].isin(prices.index)]
    span = float(prices["high"].max() - prices["low"].min())
    gap = max(span * 0.025, float(prices["close"].median()) * 0.0025)
    for side, name, color, symbol, column, offset in (
        ("BUY", "策略买入", "#ef4444", "triangle-up", "low", -gap),
        ("SELL", "策略卖出", "#22c55e", "triangle-down", "high", gap),
    ):
        selected = dated.loc[dated["side"].eq(side)] if not dated.empty else dated
        dates = selected.get("date", [])
        y = [float(prices.loc[date, column]) + offset for date in dates]
        custom = (
            selected[["signal_date", "quantity", "price", "fees", "trigger"]].to_numpy()
            if not selected.empty
            else []
        )
        figure.add_trace(
            go.Scatter(
                x=dates,
                y=y,
                mode="markers",
                name=name,
                customdata=custom,
                marker={"color": color, "symbol": symbol, "size": 14},
                hovertemplate=(
                    "%{x|%Y-%m-%d}<br>信号日 %{customdata[0]}"
                    "<br>数量 %{customdata[1]:,.0f}<br>成交价 %{customdata[2]:.3f}"
                    "<br>费用 %{customdata[3]:.2f}<br>触发 %{customdata[4]}"
                    f"<extra>{name}</extra>"
                ),
            ),
            row=1,
            col=1,
        )


def render_backtest_chart_html(
    signal_replay: SignalReplay,
    replay_data: ReplayData,
    result: BacktestResult,
) -> str:
    """Render the audited replay as the full interactive backtest chart."""
    prices = _normalize_daily(replay_data.adjusted.daily).loc[
        signal_replay.evaluation_start : signal_replay.evaluation_end
    ]
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.035,
        row_heights=[0.76, 0.24],
    )
    figure.add_trace(
        go.Candlestick(
            x=prices.index,
            open=prices["open"],
            high=prices["high"],
            low=prices["low"],
            close=prices["close"],
            name="日K",
            increasing_line_color="#ef4444",
            decreasing_line_color="#22c55e",
            hoverinfo="skip",
        ),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=prices.index,
            y=prices["close"],
            mode="markers",
            name="日K数据",
            showlegend=False,
            marker={"color": "rgba(0,0,0,0)", "size": 12},
            customdata=prices[["open", "high", "low", "close"]].to_numpy(),
            hovertemplate=(
                "开 %{customdata[0]:.3f}<br>高 %{customdata[1]:.3f}"
                "<br>低 %{customdata[2]:.3f}<br>收 %{customdata[3]:.3f}<extra>日K</extra>"
            ),
        ),
        row=1,
        col=1,
    )
    pens = extract_pen_points(replay_data.adjusted.daily, signal_replay.evaluation_end)
    pens = pens.loc[pens["dt"].between(prices.index.min(), prices.index.max())]
    figure.add_trace(
        go.Scatter(
            x=pens.get("dt", []),
            y=pens.get("price", []),
            mode="lines+markers",
            name="CZSC笔",
            line={"color": "#38bdf8", "width": 2},
            marker={"size": 5},
            hovertemplate="%{x|%Y-%m-%d}<br>笔端点 %{y:.3f}<extra>CZSC笔</extra>",
        ),
        row=1,
        col=1,
    )
    _fill_markers(figure, result.fills, prices)

    account = result.account_daily.set_index("date").loc[prices.index]
    figure.add_trace(
        go.Scatter(
            x=account.index,
            y=account["target_position"].astype(int),
            mode="lines",
            line_shape="hv",
            name="目标持仓",
            line={"color": "#bef264", "width": 2},
        ),
        row=2,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=account.index,
            y=account["quantity"].gt(0).astype(int),
            mode="lines",
            line_shape="hv",
            name="实际持仓",
            line={"color": "#e879f9", "width": 2},
        ),
        row=2,
        col=1,
    )
    figure.update_layout(
        title=f"{result.identity.reference} 确定性回测",
        height=720,
        template="plotly_dark",
        paper_bgcolor="#07101d",
        plot_bgcolor="#0e1928",
        font={"color": "#eef5ff"},
        hovermode="x unified",
        hoversubplots="axis",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
        margin={"l": 80, "r": 30, "t": 85, "b": 45},
        xaxis_rangeslider_visible=False,
    )
    figure.update_xaxes(
        rangebreaks=[{"values": _missing_calendar_dates(prices.index), "dvalue": 86_400_000}],
        gridcolor="#23344b",
        zerolinecolor="#23344b",
    )
    figure.update_yaxes(gridcolor="#23344b", zerolinecolor="#23344b")
    figure.update_yaxes(title_text="后复权价格", row=1, col=1)
    figure.update_yaxes(title_text="持仓状态", range=[-0.1, 1.1], row=2, col=1)
    html = figure.to_html(full_html=True, include_plotlyjs=True)
    style = (
        "<style>html,body{margin:0;width:100%;height:100%;overflow:hidden;"
        "background:#07101d}.plotly-graph-div{overflow:hidden}</style>"
    )
    return html.replace("</head>", f"{style}</head>", 1)
