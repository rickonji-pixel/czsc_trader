from __future__ import annotations

import pandas as pd

from czsc_trader import intraday_signal_census as census_module


def test_intraday_census_deduplicates_same_state_to_one_event_per_day(monkeypatch):
    timestamps = pd.to_datetime(
        [
            "2026-01-05 09:45",
            "2026-01-05 10:00",
            "2026-01-05 10:15",
            "2026-01-05 10:30",
            "2026-01-06 09:45",
            "2026-01-06 10:00",
            "2026-01-06 10:15",
            "2026-01-06 10:30",
        ]
    )
    frame = pd.DataFrame(
        {
            "Date": timestamps,
            "Open": 1.0,
            "High": 1.1,
            "Low": 0.9,
            "Close": 1.0,
            "Volume": 100.0,
            "Amount": 100.0,
        }
    )
    raw = pd.Series(
        ["看多_任意_任意_0", "看空_任意_任意_0"] * 4,
        index=timestamps,
    )

    def fake_evaluate(*args, **kwargs):
        return {
            "demo_signal": (
                raw,
                {"name": "demo_signal", "freq": "15分钟"},
                "15分钟_D1_测试V000001",
            )
        }, []

    monkeypatch.setattr(census_module, "_evaluate_batch", fake_evaluate)
    regimes = pd.Series(
        ["range", "trend_up"],
        index=pd.to_datetime(["2026-01-05", "2026-01-06"]),
    )
    result = census_module.generate_intraday_signal_census(
        frame,
        "510500.SH",
        regimes,
        evaluation_start="2026-01-05",
        warmup_bars=1,
        window_sessions=2,
        median_event_days_required=2,
        p10_event_days_required=2,
        distinct_event_days_required=2,
        registry=[{"name": "demo_signal", "namespace": "demo", "category": "kline"}],
    )

    assert len(result.events) == 4
    assert result.events.groupby(["state_id", "event_date"]).size().max() == 1
    assert result.states["event_days"].tolist() == [2, 2]
    assert result.states["density_capable"].tolist() == [True, True]
