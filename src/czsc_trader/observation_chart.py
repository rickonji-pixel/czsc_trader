"""Pure in-memory renderer for PTE account observation charts."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import math
from typing import Any

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from czsc_trader.charting import (
    _missing_calendar_dates,
    _normalize_daily,
    extract_pen_points,
)


CONTRACT_VERSION = "account_observation.v1"
CHART_TOTAL_HEIGHT = 540
CHART_KLINE_HEIGHT = 320
CHART_POSITION_HEIGHT = 90
CHART_VERTICAL_GAP = 10
CHART_TOP_MARGIN = 85
CHART_BOTTOM_MARGIN = 35
ALLOWED_TOP_LEVEL = {
    "contract_version",
    "account",
    "context_sessions",
    "market_data",
    "decisions",
    "intents",
    "orders",
    "fills",
    "snapshots",
}
_ACCOUNT_FIELDS = {
    "account_id",
    "strategy_id",
    "strategy_version",
    "release_id",
    "release_hash",
    "selection_data_cutoff",
    "symbol",
}
_LIST_FIELDS = ("decisions", "intents", "orders", "fills", "snapshots")


def _iso_date(value: object, field: str) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO date") from exc


def _finite(value: object, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    number = _finite(value, field)
    if not number.is_integer():
        raise ValueError(f"{field} must be an integer")
    return int(number)


def _event_date(row: dict[str, Any], collection: str) -> str | None:
    candidates = {
        "decisions": ("signal_date",),
        "intents": ("valid_session", "session"),
        "orders": ("submitted_at", "created_at", "valid_session", "session"),
        "fills": ("occurred_at", "created_at", "session"),
        "snapshots": ("session", "snapshot_date", "created_at"),
    }[collection]
    for field in candidates:
        if row.get(field) not in (None, ""):
            return _iso_date(row[field], f"{collection}.{field}")
    return None


def validate_observation(payload: object) -> dict[str, object]:
    """Return a normalized copy of an ``account_observation.v1`` document."""
    if not isinstance(payload, dict):
        raise ValueError("observation payload must be an object")
    unknown = set(payload) - ALLOWED_TOP_LEVEL
    if unknown:
        raise ValueError(f"unknown top-level fields: {sorted(unknown)}")
    missing = ALLOWED_TOP_LEVEL - set(payload)
    if missing:
        raise ValueError(f"missing top-level fields: {sorted(missing)}")
    if payload.get("contract_version") != CONTRACT_VERSION:
        raise ValueError(f"contract_version must be {CONTRACT_VERSION}")

    normalized = deepcopy(payload)
    account = normalized.get("account")
    if not isinstance(account, dict) or not _ACCOUNT_FIELDS.issubset(account):
        raise ValueError("account identity is incomplete")
    for field in _ACCOUNT_FIELDS - {"selection_data_cutoff"}:
        if not str(account.get(field, "")).strip():
            raise ValueError(f"account.{field} is required")
    cutoff = _iso_date(account["selection_data_cutoff"], "account.selection_data_cutoff")
    account["selection_data_cutoff"] = cutoff
    context_sessions = _integer(normalized.get("context_sessions"), "context_sessions")
    if context_sessions <= 0:
        raise ValueError("context_sessions must be positive")
    normalized["context_sessions"] = context_sessions

    market = normalized.get("market_data")
    if not isinstance(market, dict):
        raise ValueError("market_data must be an object")
    if market.get("adjustment") != "hfq":
        raise ValueError("market_data.adjustment must be hfq")
    if not str(market.get("manifest_sha256", "")).strip():
        raise ValueError("market_data.manifest_sha256 is required")
    bars = market.get("bars")
    if not isinstance(bars, list) or not bars:
        raise ValueError("market_data.bars must be a non-empty list")
    prior_date: str | None = None
    for index, bar in enumerate(bars):
        if not isinstance(bar, dict):
            raise ValueError(f"market_data.bars[{index}] must be an object")
        bar_date = _iso_date(bar.get("date"), f"market_data.bars[{index}].date")
        if prior_date is not None and bar_date <= prior_date:
            raise ValueError("market_data.bars dates must be strictly ascending and unique")
        prior_date = bar_date
        bar["date"] = bar_date
        for field in ("open", "high", "low", "close"):
            bar[field] = _finite(bar.get(field), f"market_data.bars[{index}].{field}")
        if not (
            bar["low"] <= bar["open"] <= bar["high"]
            and bar["low"] <= bar["close"] <= bar["high"]
        ):
            raise ValueError(f"market_data.bars[{index}] has invalid OHLC")

    account_id = str(account["account_id"])
    for collection in _LIST_FIELDS:
        rows = normalized.get(collection)
        if not isinstance(rows, list):
            raise ValueError(f"{collection} must be a list")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"{collection}[{index}] must be an object")
            if row.get("account_id") not in (None, account_id):
                raise ValueError(f"{collection}[{index}].account_id does not match account_id")
            row["account_id"] = account_id
            observed = _event_date(row, collection)
            if observed is None:
                raise ValueError(f"{collection}[{index}] has no observation date")
            if observed <= cutoff:
                raise ValueError(f"{collection}[{index}] occurs on or before cutoff")
            for field in ("quantity", "target_quantity"):
                if field in row and row[field] is not None:
                    row[field] = _integer(row[field], f"{collection}[{index}].{field}")
            for field in ("limit_price", "price", "fee"):
                if field in row and row[field] is not None:
                    row[field] = _finite(row[field], f"{collection}[{index}].{field}")
    return normalized


def _price_frame(payload: dict[str, object]) -> pd.DataFrame:
    account = payload["account"]
    frame = pd.DataFrame(payload["market_data"]["bars"])
    frame["dt"] = pd.to_datetime(frame.pop("date"))
    frame["symbol"] = str(account["symbol"])
    frame["vol"] = 0.0
    frame["amount"] = 0.0
    return _normalize_daily(frame)


def _dated_rows(rows: list[dict[str, Any]], *fields: str) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    source = next(field for field in fields if field in frame.columns)
    frame["dt"] = pd.to_datetime(frame[source].astype(str).str[:10])
    return frame.sort_values("dt")


def _marker_y(prices: pd.DataFrame, dates: pd.Series, column: str, offset: float) -> list[float]:
    return [float(prices.loc[pd.Timestamp(dt), column]) + offset for dt in dates]


def render_observation_html(
    payload: object,
    *,
    plotly_runtime: str = "embedded",
) -> str:
    """Validate and render one PTE account observation document as HTML."""
    if plotly_runtime not in {"embedded", "external"}:
        raise ValueError("plotly_runtime must be embedded or external")
    doc = validate_observation(payload)
    account = doc["account"]
    prices = _price_frame(doc)
    cutoff = pd.Timestamp(account["selection_data_cutoff"])
    span = float(prices["high"].max() - prices["low"].min())
    gap = max(span * 0.025, float(prices["close"].median()) * 0.0025)

    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=CHART_VERTICAL_GAP / (
            CHART_KLINE_HEIGHT + CHART_POSITION_HEIGHT + CHART_VERTICAL_GAP
        ),
        row_heights=[CHART_KLINE_HEIGHT, CHART_POSITION_HEIGHT],
    )
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
    pens = extract_pen_points(prices, prices.index.max())
    figure.add_trace(
        go.Scatter(
            x=pens.get("dt", []),
            y=pens.get("price", []),
            mode="lines+markers",
            name="CZSC笔",
            line={"color": "#1f77b4", "width": 2},
            marker={"size": 5},
        ),
        row=1,
        col=1,
    )

    decisions = _dated_rows(doc["decisions"], "signal_date")
    for action, name, color, symbol, column, offset in (
        ("BUY", "BUY信号", "#d62728", "triangle-up", "low", -gap),
        ("SELL", "SELL信号", "#2ca02c", "triangle-down", "high", gap),
    ):
        selected = decisions.loc[decisions.get("action", pd.Series(dtype=str)).str.upper() == action]
        dates = selected.get("dt", pd.Series(dtype="datetime64[ns]"))
        figure.add_trace(
            go.Scatter(
                x=dates,
                y=_marker_y(prices, dates, column, offset) if len(dates) else [],
                mode="markers",
                name=name,
                text=selected.get("decision_id", []),
                marker={"color": color, "symbol": symbol, "size": 14},
                hovertemplate="%{x|%Y-%m-%d}<br>%{text}<extra>" + name + "</extra>",
            ),
            row=1,
            col=1,
        )

    intents = _dated_rows(doc["intents"], "valid_session", "session")
    if not intents.empty:
        intent_dates = intents["dt"]
        valid = intent_dates.isin(prices.index)
        intents = intents.loc[valid]
        intent_dates = intents["dt"]
        intent_y = [
            float(prices.loc[dt, "low" if str(side).upper() == "BUY" else "high"])
            + (-2 * gap if str(side).upper() == "BUY" else 2 * gap)
            for dt, side in zip(intent_dates, intents["side"], strict=True)
        ]
        custom = intents[["intent_id", "quantity", "limit_price"]].to_numpy()
    else:
        intent_dates, intent_y, custom = [], [], []
    figure.add_trace(
        go.Scatter(
            x=intent_dates,
            y=intent_y,
            mode="markers",
            name="订单意图",
            customdata=custom,
            marker={"color": "#ff7f0e", "symbol": "circle-open", "size": 13, "line": {"width": 2}},
            hovertemplate=(
                "%{x|%Y-%m-%d}<br>意图 %{customdata[0]}<br>数量 %{customdata[1]}"
                "<br>未复权限价 %{customdata[2]}<extra>订单意图</extra>"
            ),
        ),
        row=1,
        col=1,
    )

    fills = _dated_rows(doc["fills"], "occurred_at", "created_at", "session")
    if not fills.empty:
        fills = fills.loc[fills["dt"].isin(prices.index)]
        fill_y = [
            float(prices.loc[dt, "low" if str(side).upper() == "BUY" else "high"])
            + (-3 * gap if str(side).upper() == "BUY" else 3 * gap)
            for dt, side in zip(fills["dt"], fills["side"], strict=True)
        ]
        fill_custom = [
            [
                row.get("fill_id", ""),
                row.get("channel_order_id", ""),
                row.get("decision_id", ""),
                row.get("quantity", 0),
                row.get("price", ""),
                row.get("fee", 0),
            ]
            for row in fills.to_dict("records")
        ]
    else:
        fill_y, fill_custom = [], []
    figure.add_trace(
        go.Scatter(
            x=fills.get("dt", []),
            y=fill_y,
            mode="markers",
            name="明确成交",
            customdata=fill_custom,
            marker={"color": "#9467bd", "symbol": "star", "size": 15},
            hovertemplate=(
                "%{x|%Y-%m-%d}<br>成交 %{customdata[0]}<br>Futu订单 %{customdata[1]}"
                "<br>决策 %{customdata[2]}<br>数量 %{customdata[3]}"
                "<br>未复权成交价 %{customdata[4]}<br>费用 %{customdata[5]}<extra>明确成交</extra>"
            ),
        ),
        row=1,
        col=1,
    )

    if not decisions.empty:
        figure.add_trace(
            go.Scatter(
                x=decisions["dt"],
                y=decisions["target_quantity"],
                mode="lines+markers",
                line_shape="hv",
                name="目标持仓",
            ),
            row=2,
            col=1,
        )
    snapshots = _dated_rows(doc["snapshots"], "session", "snapshot_date", "created_at")
    if not snapshots.empty:
        figure.add_trace(
            go.Scatter(
                x=snapshots["dt"],
                y=snapshots["quantity"],
                mode="lines+markers",
                line_shape="hv",
                name="实际持仓",
            ),
            row=2,
            col=1,
        )

    figure.add_vline(
        x=cutoff,
        line_width=2,
        line_dash="dash",
        line_color="#d62728",
        annotation_text="选择截止",
        row="all",
        col=1,
    )
    if prices.index.max() > cutoff:
        figure.add_vrect(
            x0=cutoff,
            x1=prices.index.max(),
            fillcolor="#5b9cf6",
            opacity=0.08,
            line_width=0,
            row="all",
            col=1,
        )
    figure.update_layout(
        title=f"{account['release_id']} 前瞻观察",
        height=CHART_TOTAL_HEIGHT,
        template="plotly_dark",
        paper_bgcolor="#07101d",
        plot_bgcolor="#0e1928",
        font={"color": "#eef5ff"},
        hovermode="x unified",
        hoversubplots="axis",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
        margin={
            "l": 80,
            "r": 30,
            "t": CHART_TOP_MARGIN,
            "b": CHART_BOTTOM_MARGIN,
        },
        xaxis_rangeslider_visible=False,
    )
    figure.update_xaxes(
        rangebreaks=[{"values": _missing_calendar_dates(prices.index), "dvalue": 86_400_000}],
        gridcolor="#23344b",
        zerolinecolor="#23344b",
    )
    figure.update_yaxes(
        gridcolor="#23344b", zerolinecolor="#23344b", row=1, col=1
    )
    figure.update_yaxes(
        gridcolor="#23344b", zerolinecolor="#23344b", row=2, col=1
    )
    for text, axis in (("后复权价格", figure.layout.yaxis), ("持仓数量", figure.layout.yaxis2)):
        domain = axis.domain
        figure.add_annotation(
            text=text,
            x=-0.065,
            y=(domain[0] + domain[1]) / 2,
            xref="paper",
            yref="paper",
            textangle=-90,
            showarrow=False,
            xanchor="center",
            yanchor="middle",
            font={"color": "#eef5ff", "size": 14},
        )
    include_plotlyjs: bool | str = (
        True if plotly_runtime == "embedded" else "/static/plotly.min.js"
    )
    html = figure.to_html(full_html=True, include_plotlyjs=include_plotlyjs)
    embedded_style = (
        "<style>html,body{margin:0;width:100%;height:100%;overflow:hidden;"
        "background:#07101d}.plotly-graph-div{overflow:hidden}</style>"
    )
    return html.replace("</head>", f"{embedded_style}</head>", 1)
