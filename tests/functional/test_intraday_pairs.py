from __future__ import annotations

import pandas as pd

from czsc_trader import intraday_pairs as pairs_module


def test_paired_inventory_force_closes_every_executable_unpaired_entry(monkeypatch):
    dates = pd.to_datetime(["2026-01-05", "2026-01-06"])
    fifteen_times = pd.to_datetime(
        [
            "2026-01-05 09:45",
            "2026-01-05 10:00",
            "2026-01-06 09:45",
            "2026-01-06 10:00",
        ]
    )
    fifteen = pd.DataFrame(
        {
            "Date": fifteen_times,
            "Open": 1.0,
            "High": 1.1,
            "Low": 0.9,
            "Close": 1.0,
            "Volume": 100.0,
            "Amount": 100.0,
        }
    )
    raw = pd.Series(
        ["看空_任意_任意_0", "看多_任意_任意_0"] * 2,
        index=fifteen_times,
    )

    def fake_evaluate(*args, **kwargs):
        return {
            "demo": (
                raw,
                {"name": "demo", "freq": "15分钟"},
                "15分钟_D1_测试V000001",
            )
        }, []

    monkeypatch.setattr(pairs_module, "_evaluate_batch", fake_evaluate)
    five = pd.DataFrame(
        [
            {"Date": day + pd.Timedelta(hours=10, minutes=5), "Open": 1.0, "Close": 1.0}
            for day in dates
        ]
        + [
            {"Date": day + pd.Timedelta(hours=15), "Open": 1.0, "Close": 1.01}
            for day in dates
        ]
    ).sort_values("Date")
    result = pairs_module.evaluate_paired_state_inventory(
        fifteen,
        five,
        [
            {
                "hypothesis_id": "M01",
                "mechanism": "demo",
                "signal_name": "demo",
                "entry_state": "看多",
                "exit_state": "看空",
            }
        ],
        symbol="510500.SH",
        evaluation_start=dates[0],
        warmup_bars=1,
        baseline_one_way_cost=0.0,
        stress_one_way_cost=0.0,
        base_fraction=0.5,
        density_window_sessions=1,
        median_episodes_required=1,
        p10_episodes_required=1,
        force_close_unpaired_entries=True,
    )

    assert len(result.episodes) == 2
    assert result.episodes["exit_reason"].eq("forced_close").all()
    assert result.episodes["stress_return"].round(6).eq(0.01).all()
