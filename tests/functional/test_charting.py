from __future__ import annotations

from pathlib import Path
import io
import json

import pandas as pd
import pytest
from strategy_runtime.implementation_identity import load_runtime_binding

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
    strategy = {
        "account_id": "s001-v2",
        "strategy_id": "S001",
        "strategy_version": "v2",
        "reference_id": "S001-v2",
        "identity_hash": load_runtime_binding("S001-v2")["release_hash"],
        "symbol": "588080.SH",
        "configuration": {
            "rule": {"entry_threshold": 0.175, "exit_threshold": 0.025}
        },
    }
    return {
        "contract_version": "strategy_chart.v1",
        "mode": "FORWARD_OBSERVATION",
        "strategy": strategy,
        "window": {
            "selection_data_cutoff": "2026-09-02",
            "context_sessions": 180,
        },
        "market_data": {
            "identity": "manifest123",
            "adjustment": "hfq",
            "bars": bars,
        },
        "strategy_output": {
            "decisions": [
                {
                    "account_id": "s001-v2",
                    "decision_id": "DEC-BUY",
                    "signal_date": "2026-09-03",
                    "valid_session": "2026-09-04",
                    "action": "BUY",
                    "target_quantity": 1000,
                    "factor_score": 0.2,
                },
                {
                    "account_id": "s001-v2",
                    "decision_id": "DEC-SELL",
                    "signal_date": "2026-09-04",
                    "valid_session": "2026-09-05",
                    "action": "SELL",
                    "target_quantity": 0,
                    "factor_score": 0.01,
                },
            ]
        },
        "execution": {
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
                {"account_id": "s001-v2", "session": "2026-09-03", "quantity": 0},
                {
                    "account_id": "s001-v2",
                    "session": "2026-09-04",
                    "quantity": 1000,
                },
            ],
        },
        "render": {"format": "html", "plotly_runtime": "embedded"},
    }


def test_ft_t05_observation_chart_is_owned_by_frozen_srt() -> None:
    from czsc_trader.observation_chart import render_forward_chart_html

    html = render_forward_chart_html(_observation_payload())

    assert html.lstrip().startswith("<!doctype html>")
    assert "PTE · 前瞻观察" in html
    assert "S001-v2" in html
    assert "DEC-BUY" in html and "FILL-BUY" in html
    assert "\\u76ee\\u6807\\u6301\\u4ed3" in html
    assert "\\u5b9e\\u9645\\u6301\\u4ed3" in html
    assert "\\u7b56\\u7565\\u5f97\\u5206" in html
    assert "\\u4e70\\u5165\\u9608\\u503c" in html
    assert "\\u5356\\u51fa\\u9608\\u503c" in html
    assert '"hoverinfo":"skip"' in html
    assert '"height":620' in html
    assert '"paper_bgcolor":"#07111f"' in html
    assert '"plot_bgcolor":"#0b1727"' in html


def test_ft_t06_observation_chart_rejects_invalid_identity_and_dates() -> None:
    from strategy_runtime import validate_chart_context

    wrong_scope = _observation_payload()
    wrong_scope["strategy"]["reference_id"] = "S002-v1"
    with pytest.raises(ValueError, match="belong"):
        validate_chart_context(wrong_scope)

    duplicate_bars = _observation_payload()
    duplicate_bars["market_data"]["bars"].append(
        dict(duplicate_bars["market_data"]["bars"][-1])
    )
    with pytest.raises(ValueError, match="increasing"):
        validate_chart_context(duplicate_bars)

    invalid_number = _observation_payload()
    invalid_number["market_data"]["bars"][0]["close"] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        validate_chart_context(invalid_number)


def test_ft_t07_chart_observation_cli_writes_raw_html(monkeypatch, capsys) -> None:
    from czsc_trader.cli.main import main

    payload = _observation_payload()
    payload["render"]["plotly_runtime"] = "external"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    assert main(
        [
            "chart",
            "observation",
            "--format",
            "html",
            "--plotly-runtime",
            "external",
        ]
    ) == 0
    captured = capsys.readouterr()
    assert captured.out.lstrip().startswith("<!doctype html>")
    assert 'src="/static/plotly.min.js"' in captured.out
    assert len(captured.out.encode("utf-8")) < 100_000
    assert captured.err == ""
