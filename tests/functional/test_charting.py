from __future__ import annotations

from pathlib import Path

import pandas as pd

from czsc_trader import charting


def test_ft_t04_chart_keeps_every_session_targetable_from_price_panel(
    tmp_path: Path, monkeypatch
) -> None:
    dates = pd.to_datetime(
        ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    )
    daily = pd.DataFrame(
        {
            "dt": dates,
            "open": [1.00, 1.03, 1.08, 1.05],
            "high": [1.06, 1.11, 1.12, 1.09],
            "low": [0.98, 1.01, 1.04, 1.00],
            "close": [1.04, 1.09, 1.06, 1.02],
        }
    )
    factors = pd.DataFrame(
        {
            "structure": [0.0, 0.1, 0.2, 0.1],
            "trend": [0.2, 0.3, 0.3, 0.2],
            "volume_position": [0.1, 0.1, 0.2, 0.2],
            "factor_score": [0.1, 0.2, 0.3, 0.2],
            "target_position": [0.0, 1.0, 1.0, 0.0],
        },
        index=dates,
    )
    monkeypatch.setattr(
        charting,
        "extract_pen_points",
        lambda *_args, **_kwargs: pd.DataFrame(columns=["dt", "price"]),
    )
    monkeypatch.setattr(
        charting,
        "extract_divergence_markers",
        lambda *_args, **_kwargs: pd.DataFrame(
            columns=["dt", "side", "label", "source"]
        ),
    )

    figure = charting.build_period_chart(
        daily,
        factors,
        pd.DataFrame(),
        dates[0],
        dates[-1],
        "hover contract",
    )
    hover_targets = [trace for trace in figure.data if trace.name == "日K数据"]
    candlestick = next(trace for trace in figure.data if trace.type == "candlestick")
    output_path = tmp_path / "chart.html"
    figure.write_html(output_path)

    assert figure.layout.hovermode == "x unified"
    assert figure.layout.hoversubplots == "axis"
    assert len(hover_targets) == 1
    assert list(pd.to_datetime(hover_targets[0].x)) == list(dates)
    assert hover_targets[0].marker.color == "rgba(0,0,0,0)"
    assert hover_targets[0].showlegend is False
    assert hover_targets[0].customdata.tolist() == daily[
        ["open", "high", "low", "close"]
    ].values.tolist()
    assert candlestick.hoverinfo == "skip"
    assert output_path.is_file()
