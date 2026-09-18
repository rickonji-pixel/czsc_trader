from __future__ import annotations

from dataclasses import dataclass
from html import escape

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from czsc_trader.charting import (
    _missing_calendar_dates,
    _normalize_daily,
    extract_pen_points,
)

from .datasets import ReplayData
from .metrics import calculate_metrics
from .result import BacktestResult
from .signal_replay import SignalReplay


@dataclass(frozen=True)
class _ScoreGuide:
    name: str
    value: float
    color: str


@dataclass(frozen=True)
class _ScorePanel:
    column: str
    trace_name: str
    axis_title: str
    color: str
    guides: tuple[_ScoreGuide, ...]
    dynamic_threshold_column: str | None = None
    line_shape: str = "linear"


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


def _chart_rows(
    signal_replay: SignalReplay,
    result: BacktestResult,
    prices: pd.DataFrame,
) -> pd.DataFrame:
    source = result.decisions if signal_replay.chart_data is None else signal_replay.chart_data
    rows = source.copy()
    if rows.empty:
        return pd.DataFrame(columns=["date"])
    date_column = "date" if "date" in rows else "signal_date"
    rows["date"] = pd.to_datetime(rows[date_column]).dt.normalize()
    return rows.loc[rows["date"].isin(prices.index)]


def _daily_hover_text(
    prices: pd.DataFrame,
    decisions: pd.DataFrame,
    fills: pd.DataFrame,
    entry_threshold: float,
    exit_threshold: float,
    *,
    chart_rows: pd.DataFrame,
    family: str,
    event_hold: bool = False,
    confirmation_threshold: float | None = None,
) -> list[str]:
    signals = _dated_signal_events(decisions, prices)
    executions = _dated_fills(fills, prices)
    score_groups = {date: group for date, group in chart_rows.groupby("date")}
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
        daily_scores = score_groups.get(day, pd.DataFrame())
        if not daily_scores.empty:
            decision = daily_scores.iloc[-1]
            if family == "S003":
                lines.append(f"资金流宽度 {float(decision['factor_score']):.3f}")
                if pd.notna(decision.get("threshold")):
                    lines.append(f"动态触发阈值 {float(decision['threshold']):.3f}")
                if pd.notna(decision.get("observed_weight_ratio")):
                    lines.append(
                        f"可观测成分权重 {float(decision['observed_weight_ratio']):.1%}"
                    )
                active = decision.get("signal_active")
                if pd.notna(active):
                    lines.append(f"事件状态 {'触发' if bool(active) else '未触发'}")
            elif family == "S002":
                active = float(decision["factor_score"]) >= entry_threshold
                lines.append(f"三连跌状态 {'触发' if active else '未触发'}")
                lines.append(f"信号激活值 {float(entry_threshold):.0f}")
            elif confirmation_threshold is None:
                lines.append(f"策略得分 {float(decision['factor_score']):.3f}")
            else:
                lines.extend([
                    f"基础得分 {float(decision['factor_score']):.3f}",
                    f"确认得分 {float(decision['confirmation_score']):.3f}",
                ])
            regime_value = decision.get("regime")
            if pd.notna(regime_value) and str(regime_value).strip():
                regime = {
                    "trend": "趋势（trend）",
                    "range": "震荡（range）",
                    "warmup": "预热（warmup）",
                }.get(str(regime_value), str(regime_value))
                lines.append(f"行情状态 {regime}")
            if family != "S003" and "threshold" in decision and pd.notna(decision["threshold"]):
                lines.append(f"动态触发阈值 {float(decision['threshold']):.3f}")
            if family in {"S002", "S003"}:
                pass
            elif event_hold:
                lines.append(f"信号激活值 {float(entry_threshold):.3f}")
            elif confirmation_threshold is not None:
                lines.extend([
                    f"基础分入场阈值 {float(entry_threshold):.3f}",
                    f"基础分退出阈值 {float(exit_threshold):.3f}",
                    f"确认分入场门槛 {float(confirmation_threshold):.3f}",
                ])
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
        if grouped_fills["side"].astype(str).str.upper().nunique() != 1:
            continue
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


def _multi_score_axis_range(
    series: list[pd.Series], thresholds: list[float]
) -> tuple[float, float]:
    values = [float(value) for item in series for value in item.dropna().astype(float)]
    values.extend(float(value) for value in thresholds)
    lower = min(values)
    upper = max(values)
    span = upper - lower
    padding = max(span * 0.1, max(abs(lower), abs(upper), 1.0) * 1e-9)
    return lower - padding, upper + padding


def _strategy_family(reference: str) -> str:
    return reference.split("-", 1)[0].upper()


def _srt_chart_contract(
    signal_replay: SignalReplay, result: BacktestResult, family: str
) -> tuple[float, float, float | None, bool]:
    """Extract presentation-only values from a frozen SRT payload."""

    payload = signal_replay.snapshot.strategy_payload
    rule = payload.get("rule")
    if not isinstance(rule, dict):
        raise ValueError("SRT chart requires a rule payload")
    if family == "S001":
        return float(rule["entry_threshold"]), float(rule["exit_threshold"]), None, False
    if family == "S002":
        return 1.0, 0.0, None, True
    if family == "S003":
        return float(result.decisions["threshold"].median()), 0.0, None, True
    if family == "S007":
        score = rule.get("score")
        if not isinstance(score, dict):
            raise ValueError("S007 chart requires a score rule")
        return (
            float(score["entry_threshold"]),
            float(score["exit_threshold"]),
            float(score["confirmation_threshold"]),
            False,
        )
    raise ValueError(f"strategy family {family} has no backtest chart presenter")


def _srt_fee_rate(signal_replay: SignalReplay) -> float:
    support = signal_replay.support_data or {}
    policy = support.get("execution_policy")
    if not isinstance(policy, dict):
        raise ValueError("SRT chart has no execution policy evidence")
    settings = policy.get("settings")
    if not isinstance(settings, dict):
        raise ValueError("SRT chart execution settings are invalid")
    if policy.get("policy_type") == "FROZEN_RULE":
        capital = settings.get("capital")
        if not isinstance(capital, dict) or "fee_rate" not in capital:
            raise ValueError("SRT frozen rule has no fee rate")
        return float(capital["fee_rate"])
    if policy.get("policy_type") == "INTRADAY_OVERLAY":
        return float(settings["one_way_cost"])
    raise ValueError(f"unsupported SRT execution policy: {policy.get('policy_type')}")


def _score_panels(
    family: str,
    entry_threshold: float,
    exit_threshold: float,
    *,
    confirmation_threshold: float | None,
) -> tuple[_ScorePanel, ...]:
    if family == "S001":
        return (
            _ScorePanel(
                "factor_score",
                "策略得分",
                "策略得分",
                "#4fa5ff",
                (
                    _ScoreGuide("买入阈值", entry_threshold, "#ef4444"),
                    _ScoreGuide("卖出阈值", exit_threshold, "#22c55e"),
                ),
            ),
        )
    if family == "S002":
        return (
            _ScorePanel(
                "factor_score",
                "事件状态",
                "事件状态",
                "#4fa5ff",
                (_ScoreGuide("信号激活值", entry_threshold, "#ef4444"),),
                line_shape="hv",
            ),
        )
    if family == "S003":
        return (
            _ScorePanel(
                "factor_score",
                "资金流宽度",
                "资金流宽度",
                "#4fa5ff",
                (),
                dynamic_threshold_column="threshold",
            ),
        )
    if family == "S004":
        return (
            _ScorePanel(
                "factor_score",
                "触发票数",
                "触发票数",
                "#4fa5ff",
                (_ScoreGuide("信号激活值", entry_threshold, "#ef4444"),),
            ),
        )
    if family == "S007" and confirmation_threshold is not None:
        return (
            _ScorePanel(
                "factor_score",
                "基础分",
                "基础分",
                "#4fa5ff",
                (
                    _ScoreGuide("基础分入场阈值", entry_threshold, "#ef4444"),
                    _ScoreGuide("基础分退出阈值", exit_threshold, "#22c55e"),
                ),
            ),
            _ScorePanel(
                "confirmation_score",
                "确认分",
                "确认分",
                "#c586ff",
                (
                    _ScoreGuide("确认门", confirmation_threshold, "#c586ff"),
                ),
            ),
        )
    raise ValueError(f"strategy family {family} has no backtest chart presenter")


def _metric_value(value: object, *, percent: bool = False) -> str:
    if value is None or pd.isna(value):
        return "不可用"
    number = float(value)
    return f"{number:+.2%}" if percent else f"{number:.3f}"


def _chart_document(
    figure: go.Figure,
    *,
    symbol: str,
    reference: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    trading_days: int,
    fee_rate: float,
    metrics: dict[str, object],
) -> str:
    plot = figure.to_html(
        full_html=False,
        include_plotlyjs=True,
        config={
            "displaylogo": False,
            "responsive": True,
            "scrollZoom": True,
        },
    )
    return_class = "positive" if float(metrics["return"]) >= 0 else "negative"
    values = (
        ("收益率", _metric_value(metrics["return"], percent=True), return_class),
        ("最大回撤", _metric_value(metrics["max_drawdown"], percent=True), ""),
        ("卡玛比率", _metric_value(metrics["calmar"]), ""),
        ("盈亏比", _metric_value(metrics["win_loss_ratio"]), ""),
        ("闭合交易", str(int(metrics["closed_trades"])), ""),
    )
    cards = "".join(
        (
            '<div class="metric">'
            f'<div class="metric-label">{escape(label)}</div>'
            f'<div class="metric-value {css_class}">{escape(value)}</div>'
            "</div>"
        )
        for label, value, css_class in values
    )
    title = (
        f"{escape(symbol)} <span>|</span> {escape(reference)} <span>|</span> "
        f"{start:%Y.%m.%d} - {end:%Y.%m.%d}"
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{{--bg:#07111f;--panel:#0b1727;--line:#20344b;--line-soft:#16283c;--text:#edf4ff;--muted:#8195ad;--blue:#4fa5ff;--green:#36d399;--red:#ff5f62}}
*{{box-sizing:border-box}}html,body{{margin:0;width:100%;background:var(--bg);color:var(--text);font-family:Inter,"Microsoft YaHei",system-ui,sans-serif}}
.shell{{width:100%;background:var(--bg)}}
.header{{display:flex;justify-content:space-between;gap:20px;padding:22px 26px 18px;border-bottom:1px solid var(--line-soft)}}
.eyebrow{{color:var(--blue);font-size:11px;font-weight:500;letter-spacing:.14em;margin-bottom:8px}}
h1{{margin:0;font-size:22px;line-height:1.35;font-weight:500;letter-spacing:-.015em}}h1 span{{color:var(--muted);font-weight:400}}
.subtitle{{margin-top:7px;color:var(--muted);font-size:12px}}.status{{color:var(--green);font-size:12px;white-space:nowrap;padding-top:5px}}
.status:before{{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);margin-right:8px}}
.metrics{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:1px;background:var(--line-soft);border-bottom:1px solid var(--line-soft)}}
.metric{{padding:14px 20px 13px;background:var(--panel)}}.metric-label{{color:var(--muted);font-size:11px;margin-bottom:7px}}.metric-value{{font-size:18px;font-weight:500;font-variant-numeric:tabular-nums}}.metric-value.positive{{color:var(--red)}}.metric-value.negative{{color:var(--green)}}
.plot{{padding:8px 14px 10px}}.plot>.plotly-graph-div{{overflow:hidden}}
@media(max-width:720px){{.header{{padding:18px}}.status{{display:none}}.metrics{{grid-template-columns:repeat(2,minmax(0,1fr))}}.metric:last-child{{grid-column:span 2}}.plot{{padding:4px}}}}
</style>
</head>
<body>
<section class="shell">
<header class="header"><div><div class="eyebrow">TDR · DETERMINISTIC BACKTEST</div><h1>{title}</h1><div class="subtitle">{trading_days} 个交易日 · 单边成本 {fee_rate * 10_000:.1f}bp · 确定性策略规则</div></div><div class="status">审计通过</div></header>
<div class="metrics">{cards}</div>
<main class="plot">{plot}</main>
</section>
<script>
(() => {{
  const chart = document.querySelector('.plot .plotly-graph-div');
  if (!chart || typeof chart.on !== 'function') return;
  chart.on('plotly_hover', event => {{
    const point = event.points && event.points[0];
    if (!point) return;
    Plotly.relayout(chart, {{
      'shapes[0].x0': point.x,
      'shapes[0].x1': point.x,
      'shapes[0].visible': true
    }});
  }});
  chart.on('plotly_unhover', () => {{
    Plotly.relayout(chart, {{'shapes[0].visible': false}});
  }});
}})();
</script>
</body>
</html>"""


def render_backtest_chart_html(
    signal_replay: SignalReplay,
    replay_data: ReplayData,
    result: BacktestResult,
    initial_cash: float,
) -> str:
    """Render the audited replay as the full interactive backtest chart."""
    prices = _normalize_daily(replay_data.adjusted.daily).loc[
        signal_replay.evaluation_start : signal_replay.evaluation_end
    ]
    family = _strategy_family(result.identity.reference)
    support = signal_replay.support_data or {}
    if support.get("mode") == "srt_input_contract":
        entry_threshold, exit_threshold, confirmation_threshold, event_hold = (
            _srt_chart_contract(signal_replay, result, family)
        )
    else:
        resolved = signal_replay.snapshot.resolved_rule
        if resolved is None:
            raise ValueError("candidate chart requires a resolved research rule")
        rule = resolved.rule
        overlay = resolved.constituent_moneyflow_intraday is not None
        causal_gate = resolved.causal_feature_gate
        event_hold = resolved.strategy in {
            "czsc_event_hold",
            "closing_dislocation_overnight",
        } or overlay
        confirmation_threshold = (
            None if causal_gate is None else causal_gate.confirmation_threshold
        )
        if overlay:
            entry_threshold = float(result.decisions["threshold"].median())
            exit_threshold = 0.0
        elif causal_gate is not None:
            entry_threshold = causal_gate.entry_threshold
            exit_threshold = causal_gate.exit_threshold
        elif resolved.closing_dislocation_overnight is not None:
            entry_threshold = float(resolved.closing_dislocation_overnight.votes_required)
            exit_threshold = 0.0
        elif event_hold:
            entry_threshold, exit_threshold = 1.0, 0.0
        else:
            if rule is None:
                raise ValueError("factor strategy has no score rule")
            entry_threshold, exit_threshold = rule.enter, rule.exit
    panels = _score_panels(
        family,
        entry_threshold,
        exit_threshold,
        confirmation_threshold=confirmation_threshold,
    )
    row_count = 1 + len(panels)
    price_height = 0.7 if row_count == 2 else 0.62
    score_height = (1.0 - price_height) / len(panels)
    figure = make_subplots(
        rows=row_count,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[price_height, *([score_height] * len(panels))],
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
                chart_rows=_chart_rows(signal_replay, result, prices),
                family=family,
                event_hold=event_hold,
                confirmation_threshold=confirmation_threshold,
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

    score_rows = _chart_rows(signal_replay, result, prices)
    panel_ranges: list[tuple[float, float]] = []
    for panel_index, panel in enumerate(panels, start=2):
        if panel.column not in score_rows:
            raise ValueError(
                f"{family} backtest chart requires decision column {panel.column}"
            )
        range_series = [score_rows[panel.column]]
        range_thresholds = [guide.value for guide in panel.guides]
        if panel.dynamic_threshold_column is not None:
            if panel.dynamic_threshold_column not in score_rows:
                raise ValueError(
                    f"{family} backtest chart requires decision column "
                    f"{panel.dynamic_threshold_column}"
                )
            range_series.append(score_rows[panel.dynamic_threshold_column])
        panel_range = _multi_score_axis_range(range_series, range_thresholds)
        panel_ranges.append(panel_range)
        figure.add_trace(
            go.Scatter(
                x=score_rows["date"],
                y=score_rows[panel.column].astype(float),
                mode="lines",
                name=panel.trace_name,
                line={
                    "color": panel.color,
                    "width": 2,
                    **({} if panel.line_shape == "linear" else {"shape": panel.line_shape}),
                },
                hoverinfo="none",
            ),
            row=panel_index,
            col=1,
        )
        if panel.dynamic_threshold_column is not None:
            figure.add_trace(
                go.Scatter(
                    x=score_rows["date"],
                    y=score_rows[panel.dynamic_threshold_column].astype(float),
                    mode="lines",
                    name="动态触发阈值",
                    line={"color": "#ef4444", "dash": "dash", "width": 1},
                    hoverinfo="none",
                ),
                row=panel_index,
                col=1,
            )
        for guide in panel.guides:
            figure.add_trace(
                go.Scatter(
                    x=[prices.index.min(), prices.index.max()],
                    y=[guide.value, guide.value],
                    mode="lines",
                    name=guide.name,
                    line={"color": guide.color, "dash": "dash", "width": 1},
                    hoverinfo="none",
                ),
                row=panel_index,
                col=1,
            )

    figure.add_shape(
        type="line",
        x0=prices.index.min(),
        x1=prices.index.min(),
        xref="x",
        y0=0,
        y1=1,
        yref="paper",
        visible=False,
        line={"color": "#8195ad", "dash": "dot", "width": 1},
        layer="above",
    )
    backtest_symbol = replay_data.adjusted.symbol
    figure.update_layout(
        height=640 if row_count == 2 else 760,
        template="plotly_dark",
        paper_bgcolor="#07111f",
        plot_bgcolor="#0b1727",
        font={"color": "#eef5ff"},
        hovermode="x unified",
        hoversubplots="axis",
        hoverlabel={
            "bgcolor": "rgba(5, 13, 24, 0.5)",
            "bordercolor": "rgba(129, 149, 173, 0.45)",
            "font": {"color": "#edf4ff"},
            "align": "left",
        },
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "x": 0,
            "font": {"size": 11, "color": "#a9b8ca"},
            "groupclick": "toggleitem",
        },
        margin={"l": 76, "r": 24, "t": 60, "b": 42},
        xaxis_rangeslider_visible=False,
        uirevision="tdr-backtest-chart-v2",
    )
    figure.update_xaxes(
        rangebreaks=[{"values": _missing_calendar_dates(prices.index), "dvalue": 86_400_000}],
        gridcolor="#16283c",
        zerolinecolor="#20344b",
        showspikes=False,
    )
    figure.update_yaxes(gridcolor="#16283c", zerolinecolor="#20344b")
    figure.update_yaxes(
        range=list(price_axis_range), title_text="后复权价格", row=1, col=1
    )
    for panel_index, (panel, panel_range) in enumerate(
        zip(panels, panel_ranges, strict=True), start=2
    ):
        figure.update_yaxes(
            range=list(panel_range),
            title_text=panel.axis_title,
            row=panel_index,
            col=1,
        )

    if support.get("mode") == "srt_input_contract":
        fee_rate = _srt_fee_rate(signal_replay)
    elif resolved.execution is not None:
        fee_rate = float(resolved.execution.capital.fee_rate)
    elif resolved.constituent_moneyflow_intraday is not None:
        fee_rate = float(resolved.constituent_moneyflow_intraday.one_way_cost)
    else:
        fee_rate = 0.0
    metrics = calculate_metrics(result, initial_cash)
    return _chart_document(
        figure,
        symbol=backtest_symbol,
        reference=result.identity.reference,
        start=prices.index.min(),
        end=prices.index.max(),
        trading_days=len(prices),
        fee_rate=fee_rate,
        metrics=metrics,
    )
