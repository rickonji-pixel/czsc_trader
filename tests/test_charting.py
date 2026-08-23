import importlib.util
from pathlib import Path
import re

import numpy as np
import pandas as pd

from czsc_trader.data import load_market_data


def _chart_inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    daily = load_market_data(Path("data/raw")).daily
    index = pd.DatetimeIndex(daily["dt"], name="dt")
    factors = pd.DataFrame(
        {
            "structure": np.zeros(len(index)),
            "trend": np.zeros(len(index)),
            "volume_position": np.zeros(len(index)),
            "factor_score": np.zeros(len(index)),
            "target_position": np.zeros(len(index)),
        },
        index=index,
    )
    factors.loc["2026-01-06":"2026-01-07", "target_position"] = 1.0
    orders = pd.DataFrame(
        {
            "signal_date": [pd.Timestamp("2025-12-31"), pd.Timestamp("2026-01-06")],
            "execution_date": [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-07")],
            "side": ["Buy", "Sell"],
            "size": [100.0, 100.0],
            "price": [1.382, 1.47],
            "fees": [0.07, 0.07],
            "factor_event_id": ["InitialEntry:sample:20251231", "Factor:20260106:Exit"],
            "event_type": ["InitialEntry", "Exit"],
        }
    )
    return daily, factors, orders


def test_factor_markers_only_emit_real_position_changes() -> None:
    """Catch markers being emitted daily instead of only when the factor position changes."""
    assert importlib.util.find_spec("czsc_trader.charting") is not None
    from czsc_trader.charting import extract_factor_markers

    index = pd.to_datetime(["2025-12-31", "2026-01-05", "2026-01-06", "2026-01-07"])
    factors = pd.DataFrame(
        {"target_position": [0.0, 0.0, 1.0, 0.0]},
        index=pd.DatetimeIndex(index, name="dt"),
    )

    markers = extract_factor_markers(
        factors,
        pd.Timestamp("2026-01-05"),
        pd.Timestamp("2026-01-07"),
    )

    assert markers.to_dict("records") == [
        {"dt": pd.Timestamp("2026-01-06"), "side": "FactorBuy"},
        {"dt": pd.Timestamp("2026-01-07"), "side": "FactorExit"},
    ]


def test_period_chart_contains_required_layers_and_respects_period_end() -> None:
    """Catch missing explanatory layers or candles leaking beyond the target period."""
    from czsc_trader.charting import build_period_chart

    daily, factors, orders = _chart_inputs()
    start = pd.Timestamp("2026-01-05")
    end = pd.Timestamp("2026-03-31")

    figure = build_period_chart(daily, factors, orders, start, end, "588080 2026Q1")

    names = {trace.name for trace in figure.data}
    assert {
        "日K",
        "CZSC笔",
        "底背驰",
        "顶背驰",
        "因子入场",
        "因子离场",
        "周期初始因子入场",
        "策略买入",
        "策略卖出",
        "structure",
        "trend",
        "volume_position",
        "factor_score",
    } <= names
    candle = next(trace for trace in figure.data if trace.name == "日K")
    candle_dates = pd.DatetimeIndex(pd.to_datetime(candle.x))
    assert candle_dates.min() == start
    assert candle_dates.max() == end
    buy = next(trace for trace in figure.data if trace.name == "策略买入")
    assert list(pd.to_datetime(buy.x)) == [pd.Timestamp("2026-01-05")]


def test_period_chart_links_hover_across_shared_date_axis() -> None:
    """Catch hover interactions being limited to only one chart panel."""
    from czsc_trader.charting import build_period_chart

    daily, factors, orders = _chart_inputs()
    figure = build_period_chart(
        daily,
        factors,
        orders,
        pd.Timestamp("2026-01-05"),
        pd.Timestamp("2026-03-31"),
        "588080 2026Q1",
    )

    assert figure.layout.hovermode == "x unified"
    assert figure.layout.hoversubplots == "axis"
    factor_traces = [trace for trace in figure.data if trace.name in {
        "structure",
        "trend",
        "volume_position",
        "factor_score",
    }]
    assert {trace.xaxis for trace in factor_traces} == {"x"}


def test_factor_entry_exit_colors_follow_trade_direction() -> None:
    """Catch factor entry/exit markers using colors opposite to buy/sell semantics."""
    from czsc_trader.charting import build_period_chart

    daily, factors, orders = _chart_inputs()
    figure = build_period_chart(
        daily,
        factors,
        orders,
        pd.Timestamp("2026-01-05"),
        pd.Timestamp("2026-03-31"),
        "588080 2026Q1",
    )

    factor_entry = next(trace for trace in figure.data if trace.name == "因子入场")
    factor_exit = next(trace for trace in figure.data if trace.name == "因子离场")
    assert factor_entry.marker.color == "#d62728"
    assert factor_exit.marker.color == "#2ca02c"


def test_price_annotations_use_separate_visual_lanes() -> None:
    """Catch factor and strategy markers obscuring each other or the daily candle."""
    from czsc_trader.charting import build_period_chart

    daily, factors, orders = _chart_inputs()
    indexed = daily.set_index("dt")
    figure = build_period_chart(
        daily,
        factors,
        orders,
        pd.Timestamp("2026-01-05"),
        pd.Timestamp("2026-03-31"),
        "588080 2026Q1",
    )

    factor_entry = next(trace for trace in figure.data if trace.name == "因子入场")
    factor_exit = next(trace for trace in figure.data if trace.name == "因子离场")
    initial_entry = next(trace for trace in figure.data if trace.name == "周期初始因子入场")
    strategy_buy = next(trace for trace in figure.data if trace.name == "策略买入")
    strategy_sell = next(trace for trace in figure.data if trace.name == "策略卖出")

    factor_entry_date = pd.Timestamp(factor_entry.x[0])
    factor_exit_date = pd.Timestamp(factor_exit.x[0])
    initial_date = pd.Timestamp(initial_entry.x[0])
    sell_date = pd.Timestamp(strategy_sell.x[0])
    assert float(factor_entry.y[0]) < float(indexed.loc[factor_entry_date, "low"])
    assert float(factor_exit.y[0]) > float(indexed.loc[factor_exit_date, "high"])
    assert float(strategy_buy.y[0]) < float(initial_entry.y[0]) < float(indexed.loc[initial_date, "low"])
    assert float(strategy_sell.y[0]) > float(indexed.loc[sell_date, "high"])
    factor_gap = float(indexed.loc[initial_date, "low"]) - float(initial_entry.y[0])
    assert float(initial_entry.y[0]) - float(strategy_buy.y[0]) >= factor_gap * 1.4
    assert float(strategy_sell.y[0]) - float(indexed.loc[sell_date, "high"]) >= factor_gap * 2.4
    assert float(strategy_buy.customdata[0][3]) == 1.382
    assert "%{customdata[3]:.3f}" in strategy_buy.hovertemplate


def test_period_chart_writer_embeds_plotly_for_offline_use(tmp_path: Path) -> None:
    """Catch chart exports that depend on a network CDN or omit the full HTML document."""
    from czsc_trader.charting import write_period_chart

    daily, factors, orders = _chart_inputs()
    output = tmp_path / "chart_2026Q1.html"
    result = write_period_chart(
        daily,
        factors,
        orders,
        pd.Timestamp("2026-01-05"),
        pd.Timestamp("2026-03-31"),
        "588080 2026Q1",
        output,
    )

    html = result.read_text(encoding="utf-8")
    assert result == output
    assert html.lstrip().lower().startswith("<html>")
    assert "plotly.js v" in html.lower()
    assert re.search(r"<script[^>]+src=[^>]*cdn\.plot\.ly", html, flags=re.IGNORECASE) is None
