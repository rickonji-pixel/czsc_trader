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
    if "action" in dated and dated["action"].eq("INTRADAY_LONG_OVERLAY").all():
        changed = pd.Series(True, index=dated.index)
    else:
        changed = dated["target_position"].ne(dated["target_position"].shift())
    return dated.loc[changed & dated["date"].isin(prices.index)]


def _daily_hover_text(
    prices: pd.DataFrame,
    decisions: pd.DataFrame,
    fills: pd.DataFrame,
    entry_threshold: float,
    exit_threshold: float,
    *,
    event_hold: bool = False,
) -> list[str]:
    dated_decisions = decisions.copy()
    dated_decisions["date"] = pd.to_datetime(dated_decisions["signal_date"]).dt.normalize()
    dated_decisions = dated_decisions.loc[dated_decisions["date"].isin(prices.index)]
    signals = _dated_signal_events(decisions, prices)
    executions = _dated_fills(fills, prices)
    decision_groups = {date: group for date, group in dated_decisions.groupby("date")}
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
        daily_decisions = decision_groups.get(day, pd.DataFrame())
        if not daily_decisions.empty:
            decision = daily_decisions.iloc[-1]
            regime = {
                "trend": "趋势（trend）",
                "range": "震荡（range）",
                "warmup": "预热（warmup）",
            }.get(str(decision.get("regime")), "不适用")
            lines.extend([
                f"策略得分 {float(decision['factor_score']):.3f}",
                f"行情状态 {regime}",
            ])
            if "threshold" in decision and pd.notna(decision["threshold"]):
                lines.append(f"动态触发阈值 {float(decision['threshold']):.3f}")
            if event_hold:
                lines.append(f"信号激活值 {float(entry_threshold):.3f}")
            else:
                lines.extend([
                    f"买入阈值 {float(entry_threshold):.3f}",
                    f"卖出阈值 {float(exit_threshold):.3f}",
                ])
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
    dated: pd.DataFrame,
) -> None:
    for side, name, color, symbol, angle in (
        ("BUY", "买入成交", "#ef4444", "triangle-down", 180),
        ("SELL", "卖出成交", "#22c55e", "triangle-down", 0),
    ):
        selected = dated.loc[dated["side"].eq(side)] if not dated.empty else dated
        dates = selected["date"].tolist() if "date" in selected else []
        figure.add_trace(
            go.Scatter(
                x=dates,
                y=selected.get("marker_y", pd.Series(dtype=float)).tolist(),
                mode="markers",
                name=name,
                marker={
                    "color": color,
                    "symbol": symbol,
                    "size": 10,
                    "line": {"width": 1},
                    "angle": angle,
                    "angleref": "up",
                },
                hoverinfo="skip",
            ),
            row=1,
            col=1,
        )


def _signal_markers(
    figure: go.Figure,
    dated: pd.DataFrame,
) -> None:
    for target, name, color, symbol, angle in (
        (1, "买入信号", "#ef4444", "triangle-down-open", 180),
        (0, "卖出信号", "#22c55e", "triangle-down-open", 0),
    ):
        selected = dated.loc[dated["target_position"].eq(target)]
        dates = selected["date"].tolist()
        figure.add_trace(
            go.Scatter(
                x=dates,
                y=selected["marker_y"].tolist(),
                mode="markers",
                name=name,
                marker={
                    "color": color,
                    "symbol": symbol,
                    "size": 10,
                    "line": {"width": 1},
                    "angle": angle,
                    "angleref": "up",
                },
                hoverinfo="skip",
            ),
            row=1,
            col=1,
        )


def _paired_event_markers(
    decisions: pd.DataFrame,
    fills: pd.DataFrame,
    prices: pd.DataFrame,
    pens: pd.DataFrame,
    price_axis_range: tuple[float, float],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    signals = _dated_signal_events(decisions, prices).copy()
    executions = _dated_fills(fills, prices).copy()
    axis_low, axis_high = price_axis_range
    pen_curve = pd.Series(index=prices.index, dtype=float)
    if not pens.empty:
        points = pens.copy()
        points["dt"] = pd.to_datetime(points["dt"]).dt.normalize()
        points = points.loc[points["dt"].isin(prices.index)].drop_duplicates("dt", keep="last")
        pen_curve.loc[points["dt"]] = points["price"].astype(float).to_numpy()
        pen_curve = pen_curve.interpolate(method="linear", limit_area="inside")

    def outside_value(date: pd.Timestamp, side: str) -> float:
        column = "low" if side == "BUY" else "high"
        values = [float(prices.loc[date, column])]
        if pd.notna(pen_curve.get(date)):
            values.append(float(pen_curve.loc[date]))
        boundary = min(values) if side == "BUY" else max(values)
        axis_edge = axis_low if side == "BUY" else axis_high
        return boundary + (axis_edge - boundary) * 0.2

    signals["marker_y"] = [
        outside_value(row.date, "BUY" if int(row.target_position) == 1 else "SELL")
        for row in signals.itertuples()
    ]
    executions["marker_y"] = [
        outside_value(row.date, str(row.side).upper())
        for row in executions.itertuples()
    ]
    if signals.empty or executions.empty:
        return signals, executions

    for decision_id, grouped_fills in executions.groupby("decision_id"):
        signal_indexes = signals.index[signals["decision_id"].eq(decision_id)]
        side = str(grouped_fills.iloc[0]["side"]).upper()
        column = "low" if side == "BUY" else "high"
        signal_dates = pd.to_datetime(grouped_fills["signal_date"]).dt.normalize()
        dates = signal_dates.loc[signal_dates.isin(prices.index)].tolist()
        dates.extend(grouped_fills["date"].tolist())
        start, end = min(dates), max(dates)
        values = prices.loc[start:end, column].astype(float).tolist()
        values.extend(pen_curve.loc[start:end].dropna().astype(float).tolist())
        boundary = min(values) if side == "BUY" else max(values)
        axis_edge = axis_low if side == "BUY" else axis_high
        shared_y = boundary + (axis_edge - boundary) * 0.2
        if not signal_indexes.empty:
            signals.loc[signal_indexes, "marker_y"] = shared_y
        executions.loc[grouped_fills.index, "marker_y"] = shared_y
    return signals, executions


def _price_axis_range(prices: pd.DataFrame, pens: pd.DataFrame) -> tuple[float, float]:
    values = prices[["low", "high"]].astype(float).to_numpy().ravel().tolist()
    if not pens.empty:
        values.extend(pens["price"].astype(float).tolist())
    lower = min(values)
    upper = max(values)
    span = upper - lower
    padding = max(span * 0.1, max(abs(lower), abs(upper), 1.0) * 1e-9)
    return lower - padding, upper + padding


def _score_axis_range(scores: pd.Series, entry: float, exit_: float) -> tuple[float, float]:
    lower = min(float(scores.min()), float(entry), float(exit_))
    upper = max(float(scores.max()), float(entry), float(exit_))
    span = upper - lower
    padding = max(span * 0.1, max(abs(lower), abs(upper), 1.0) * 1e-9)
    return lower - padding, upper + padding


def render_backtest_chart_html(
    signal_replay: SignalReplay,
    replay_data: ReplayData,
    result: BacktestResult,
) -> str:
    """Render the audited replay as the full interactive backtest chart."""
    prices = _normalize_daily(replay_data.adjusted.daily).loc[
        signal_replay.evaluation_start : signal_replay.evaluation_end
    ]
    resolved = signal_replay.snapshot.resolved_rule
    rule = resolved.rule
    overlay = resolved.constituent_moneyflow_intraday is not None
    event_hold = resolved.strategy == "czsc_event_hold" or overlay
    if overlay:
        entry_threshold = float(result.decisions["threshold"].median())
        exit_threshold = 0.0
    elif event_hold:
        entry_threshold, exit_threshold = 1.0, 0.0
    else:
        if rule is None:
            raise ValueError("factor strategy has no score rule")
        entry_threshold, exit_threshold = rule.enter, rule.exit
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
            text=_daily_hover_text(
                prices,
                result.decisions,
                result.fills,
                entry_threshold,
                exit_threshold,
                event_hold=event_hold,
            ),
            hovertemplate="%{text}<extra></extra>",
        ),
        row=1,
        col=1,
    )
    pens = extract_pen_points(replay_data.adjusted.daily, signal_replay.evaluation_end)
    pens = pens.loc[pens["dt"].between(prices.index.min(), prices.index.max())]
    price_axis_range = _price_axis_range(prices, pens)
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
    signals, executions = _paired_event_markers(
        result.decisions,
        result.fills,
        prices,
        pens,
        price_axis_range,
    )
    _signal_markers(figure, signals)
    _fill_markers(figure, executions)

    score_rows = result.decisions.copy()
    score_rows["date"] = pd.to_datetime(score_rows["signal_date"]).dt.normalize()
    score_rows = score_rows.loc[score_rows["date"].isin(prices.index)]
    score_axis_range = _score_axis_range(
        score_rows["factor_score"], entry_threshold, exit_threshold
    )
    figure.add_trace(
        go.Scatter(
            x=score_rows["date"],
            y=score_rows["factor_score"].astype(float),
            mode="lines",
            name="策略得分",
            line={"color": "#fbbf24", "width": 2},
            hoverinfo="skip",
        ),
        row=2,
        col=1,
    )
    if overlay:
        figure.add_trace(
            go.Scatter(
                x=score_rows["date"],
                y=score_rows["threshold"].astype(float),
                mode="lines",
                name="动态触发阈值",
                line={"color": "#ef4444", "dash": "dash", "width": 1},
                hoverinfo="skip",
            ),
            row=2,
            col=1,
        )
    guides = (
        ()
        if overlay
        else (("信号激活值", entry_threshold, "#ef4444"),)
        if event_hold
        else (
            ("买入阈值", entry_threshold, "#ef4444"),
            ("卖出阈值", exit_threshold, "#22c55e"),
        )
    )
    for name, value, color in guides:
        figure.add_trace(
            go.Scatter(
                x=[prices.index.min(), prices.index.max()],
                y=[float(value), float(value)],
                mode="lines",
                name=name,
                line={"color": color, "dash": "dash", "width": 1},
                hoverinfo="skip",
            ),
            row=2,
            col=1,
        )
    figure.update_traces(xaxis="x", row=2, col=1)
    backtest_symbol = str(signal_replay.snapshot.resolved_rule.symbol)
    figure.update_layout(
        title=(
            f"{backtest_symbol} | {result.identity.reference} | "
            f"{prices.index.min():%Y.%m.%d} - {prices.index.max():%Y.%m.%d}"
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
        showspikes=True,
        spikecolor="#64748b",
        spikedash="dot",
        spikemode="across",
        spikesnap="data",
        spikethickness=1,
    )
    figure.update_yaxes(gridcolor="#23344b", zerolinecolor="#23344b")
    figure.update_yaxes(range=list(price_axis_range), row=1, col=1)
    figure.update_yaxes(range=list(score_axis_range), row=2, col=1)
    for title, axis_name in (("后复权价格", "yaxis"), ("策略得分", "yaxis2")):
        domain = figure.layout[axis_name].domain
        figure.add_annotation(
            text=title,
            x=-0.025,
            y=sum(domain) / 2,
            xref="paper",
            yref="paper",
            textangle=-90,
            showarrow=False,
            xanchor="center",
            yanchor="middle",
        )
    html = figure.to_html(full_html=True, include_plotlyjs=True)
    style = (
        "<style>html,body{margin:0;width:100%;height:100%;overflow:hidden;"
        "background:#07101d}.plotly-graph-div{overflow:hidden}</style>"
    )
    return html.replace("</head>", f"{style}</head>", 1)
