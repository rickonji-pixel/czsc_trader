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
            "base_target_position": np.zeros(len(index)),
        },
        index=index,
    )
    factors.loc["2026-01-06":"2026-01-07", "base_target_position"] = 1.0
    orders = pd.DataFrame(
        {
            "signal_date": [pd.Timestamp("2025-12-31"), pd.Timestamp("2026-01-06")],
            "execution_date": [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-07")],
            "side": ["Buy", "Sell"],
            "size": [100.0, 100.0],
            "price": [1.382, 1.47],
            "fees": [0.07, 0.07],
        }
    )
    return daily, factors, orders


def test_factor_markers_only_emit_real_position_changes() -> None:
    """Catch markers being emitted daily instead of only when the factor position changes."""
    assert importlib.util.find_spec("czsc_trader.charting") is not None
    from czsc_trader.charting import extract_factor_markers

    index = pd.to_datetime(["2025-12-31", "2026-01-05", "2026-01-06", "2026-01-07"])
    factors = pd.DataFrame(
        {"base_target_position": [0.0, 0.0, 1.0, 0.0]},
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
