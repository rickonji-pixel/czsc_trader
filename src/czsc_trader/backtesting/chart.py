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


def _dated_fills(fills: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    if fills.empty:
        return fills.copy()
    dated = fills.copy()
    dated["date"] = pd.to_datetime(dated["fill_time"]).dt.normalize()
    return dated.loc[dated["date"].isin(prices.index)]


def _dated_signal_events(decisions: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    dated = decisions.copy().sort_values("signal_date")
    dated["date"] = pd.to_datetime(dated["signal_date"]).dt.normalize()
    changed = dated["target_position"].ne(dated["target_position"].shift())
    return dated.loc[changed & dated["date"].isin(prices.index)]


def _daily_hover_text(
    prices: pd.DataFrame,
    decisions: pd.DataFrame,
    fills: pd.DataFrame,
) -> list[str]:
    signals = _dated_signal_events(decisions, prices)
    executions = _dated_fills(fills, prices)
    signal_groups = {date: group for date, group in signals.groupby("date")}
    fill_groups = {date: group for date, group in executions.groupby("date")}
    output: list[str] = []
    for day, row in prices.iterrows():
        lines = [
            f"<b>{day.date()}</b>",
            f"开 {float(row['open']):.3f}",
            f"高 {float(row['high']):.3f}",
            f"低 {float(row['low']):.3f}",
            f"收 {float(row['close']):.3f}",
        ]
        for signal in signal_groups.get(day, pd.DataFrame()).to_dict("records"):
            direction = "买入信号" if int(signal["target_position"]) == 1 else "卖出信号"
            lines.append(f"<b>{direction}</b> · 决策 {signal['decision_id']}")
        for fill in fill_groups.get(day, pd.DataFrame()).to_dict("records"):
            direction = "买入成交" if str(fill["side"]).upper() == "BUY" else "卖出成交"
            lines.append(
                f"<b>{direction}</b> · 数量 {int(fill['quantity']):,}"
                f" · 未复权价 {float(fill['price']):.3f}"
                f" · 费用 {float(fill['fees']):.2f} · 触发 {fill['trigger']}"
            )
        output.append("<br>".join(lines))
    return output


def _fill_markers(
    figure: go.Figure,
    fills: pd.DataFrame,
    prices: pd.DataFrame,
) -> None:
    dated = _dated_fills(fills, prices)
    for side, name, color, symbol, glyph, column, yshift in (
        ("BUY", "买入成交", "#ef4444", "triangle-up", "▲", "low", -18),
        ("SELL", "卖出成交", "#22c55e", "triangle-down", "▼", "high", 18),
    ):
        selected = dated.loc[dated["side"].eq(side)] if not dated.empty else dated
        figure.add_trace(
            go.Scatter(
                x=[None],
                y=[None],
                mode="markers",
                name=name,
                marker={
                    "color": color,
                    "symbol": symbol,
                    "size": 10,
                    "line": {"width": 1},
                },
                hoverinfo="skip",
            ),
            row=1,
            col=1,
        )
        for date in selected.get("date", []):
            figure.add_annotation(
                x=date,
                y=float(prices.loc[date, column]),
                text=glyph,
                name=name,
                showarrow=False,
                yshift=yshift,
                font={"color": color, "size": 10},
                row=1,
                col=1,
            )


def _signal_markers(
    figure: go.Figure,
    decisions: pd.DataFrame,
    prices: pd.DataFrame,
) -> None:
    dated = _dated_signal_events(decisions, prices)
    for target, name, color, symbol, glyph, column, yshift in (
        (1, "买入信号", "#ef4444", "triangle-up-open", "△", "low", -8),
        (0, "卖出信号", "#22c55e", "triangle-down-open", "▽", "high", 8),
    ):
        selected = dated.loc[dated["target_position"].eq(target)]
        dates = selected["date"].tolist()
        figure.add_trace(
            go.Scatter(
                x=[None],
                y=[None],
                mode="markers",
                name=name,
                marker={
                    "color": color,
                    "symbol": symbol,
                    "size": 10,
                    "line": {"width": 1},
                },
                hoverinfo="skip",
            ),
            row=1,
            col=1,
        )
        for date in dates:
            figure.add_annotation(
                x=date,
                y=float(prices.loc[date, column]),
                text=glyph,
                name=name,
                showarrow=False,
                yshift=yshift,
                font={"color": color, "size": 10},
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
            name="交易日详情",
            showlegend=False,
            marker={"color": "rgba(0,0,0,0)", "size": 12},
            text=_daily_hover_text(prices, result.decisions, result.fills),
            hovertemplate="%{text}<extra></extra>",
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
            line={"color": "#38bdf8", "width": 1},
            marker={"size": 3},
            hovertemplate="%{x|%Y-%m-%d}<br>笔端点 %{y:.3f}<extra>CZSC笔</extra>",
        ),
        row=1,
        col=1,
    )
    _signal_markers(figure, result.decisions, prices)
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
        title=(
            f"{result.identity.reference} 确定性回测｜"
            f"{prices.index.min().date()}—{prices.index.max().date()}"
        ),
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
