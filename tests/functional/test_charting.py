from __future__ import annotations

from pathlib import Path
import io
import json

import pandas as pd
import pytest

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


def _observation_payload() -> dict[str, object]:
    bars = []
    for index, dt in enumerate(pd.bdate_range("2026-08-24", "2026-09-04")):
        close = 1.0 + index * 0.01
        bars.append(
            {
                "date": dt.date().isoformat(),
                "open": close - 0.01,
                "high": close + 0.02,
                "low": close - 0.02,
                "close": close,
            }
        )
    account = {
        "account_id": "s001-v2",
        "strategy_id": "S001",
        "strategy_version": "v2",
        "release_id": "S001-v2",
        "release_hash": "abc123",
        "selection_data_cutoff": "2026-09-02",
        "symbol": "588080.SH",
    }
    return {
        "contract_version": "account_observation.v1",
        "account": account,
        "context_sessions": 180,
        "market_data": {
            "manifest_sha256": "manifest123",
            "adjustment": "hfq",
            "bars": bars,
        },
        "decisions": [
            {
                "account_id": "s001-v2",
                "decision_id": "DEC-BUY",
                "signal_date": "2026-09-03",
                "valid_session": "2026-09-04",
                "action": "BUY",
                "target_quantity": 1000,
            },
            {
                "account_id": "s001-v2",
                "decision_id": "DEC-SELL",
                "signal_date": "2026-09-04",
                "valid_session": "2026-09-05",
                "action": "SELL",
                "target_quantity": 0,
            },
        ],
        "intents": [
            {
                "account_id": "s001-v2",
                "intent_id": "INT-BUY",
                "decision_id": "DEC-BUY",
                "valid_session": "2026-09-04",
                "side": "BUY",
                "quantity": 1000,
                "limit_price": 1.08,
            }
        ],
        "orders": [],
        "fills": [
            {
                "account_id": "s001-v2",
                "fill_id": "FILL-BUY",
                "decision_id": "DEC-BUY",
                "channel_order_id": "FUTU-1",
                "occurred_at": "2026-09-04T09:31:00+08:00",
                "side": "BUY",
                "quantity": 1000,
                "price": 1.08,
                "fee": 1.0,
            }
        ],
        "snapshots": [
            {
                "account_id": "s001-v2",
                "session": "2026-09-03",
                "quantity": 0,
            },
            {
                "account_id": "s001-v2",
                "session": "2026-09-04",
                "quantity": 1000,
            },
        ],
    }


def test_ft_t05_observation_chart_is_pure_account_scoped_html(monkeypatch) -> None:
    from czsc_trader import observation_chart

    monkeypatch.setattr(
        observation_chart,
        "extract_pen_points",
        lambda *_args, **_kwargs: pd.DataFrame(columns=["dt", "price"]),
    )
    html = observation_chart.render_observation_html(_observation_payload())

    assert html.lstrip().startswith("<html>")
    assert "\\u9009\\u62e9\\u622a\\u6b62" in html
    assert "DEC-BUY" in html and "INT-BUY" in html and "FILL-BUY" in html
    assert "\\u76ee\\u6807\\u6301\\u4ed3" in html
    assert "\\u5b9e\\u9645\\u6301\\u4ed3" in html
    assert '"hoverinfo":"skip"' in html
    assert '"height":540' in html
    assert '"paper_bgcolor":"#07101d"' in html
    assert '"plot_bgcolor":"#0e1928"' in html
    assert observation_chart.CHART_TOTAL_HEIGHT == 540
    assert observation_chart.CHART_KLINE_HEIGHT == 320
    assert observation_chart.CHART_POSITION_HEIGHT == 90
    assert "html,body{margin:0;width:100%;height:100%;overflow:hidden" in html

    plot_call = html.rindex("Plotly.newPlot(") + len("Plotly.newPlot(")
    decoder = json.JSONDecoder()
    cursor = plot_call
    while html[cursor].isspace():
        cursor += 1
    _, cursor = decoder.raw_decode(html, cursor)
    cursor = html.index(",", cursor) + 1
    while html[cursor].isspace():
        cursor += 1
    _, cursor = decoder.raw_decode(html, cursor)
    cursor = html.index(",", cursor) + 1
    while html[cursor].isspace():
        cursor += 1
    layout, _ = decoder.raw_decode(html, cursor)
    y_titles = {
        item["text"]: item
        for item in layout["annotations"]
        if item.get("text") in {"后复权价格", "持仓数量"}
    }
    assert set(y_titles) == {"后复权价格", "持仓数量"}
    assert y_titles["后复权价格"]["x"] == y_titles["持仓数量"]["x"]
    assert y_titles["后复权价格"]["xref"] == "paper"
    assert y_titles["持仓数量"]["xref"] == "paper"


def test_ft_t06_observation_chart_rejects_invalid_identity_and_dates() -> None:
    from czsc_trader.observation_chart import validate_observation

    wrong_scope = _observation_payload()
    wrong_scope["decisions"][0]["account_id"] = "another"
    with pytest.raises(ValueError, match="account_id"):
        validate_observation(wrong_scope)

    duplicate_bars = _observation_payload()
    duplicate_bars["market_data"]["bars"].append(
        dict(duplicate_bars["market_data"]["bars"][-1])
    )
    with pytest.raises(ValueError, match="ascending"):
        validate_observation(duplicate_bars)

    before_cutoff = _observation_payload()
    before_cutoff["decisions"][0]["signal_date"] = "2026-09-02"
    with pytest.raises(ValueError, match="cutoff"):
        validate_observation(before_cutoff)


def test_ft_t07_chart_observation_cli_writes_raw_html(monkeypatch, capsys) -> None:
    from czsc_trader import observation_chart
    from czsc_trader.cli.main import main

    monkeypatch.setattr(
        observation_chart,
        "extract_pen_points",
        lambda *_args, **_kwargs: pd.DataFrame(columns=["dt", "price"]),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_observation_payload())))

    assert main(["chart", "observation", "--format", "html"]) == 0
    captured = capsys.readouterr()
    assert captured.out.lstrip().startswith("<html>")
    assert captured.err == ""
