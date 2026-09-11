from __future__ import annotations

import pandas as pd

from czsc_trader.intraday_mechanism import evaluate_intraday_mechanisms


def test_intraday_mechanism_uses_next_bar_open_and_next_session_exit():
    dates = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
    five_rows = []
    fifteen_rows = []
    for number, day in enumerate(dates):
        base = 1.0 + number * 0.01
        five_rows.extend(
            [
                {"Date": day + pd.Timedelta(hours=9, minutes=45), "Open": base, "Close": base},
                {
                    "Date": day + pd.Timedelta(hours=10, minutes=5),
                    "Open": base + 0.001,
                    "Close": base + 0.002,
                },
            ]
        )
        fifteen_rows.append({"Date": day + pd.Timedelta(hours=10)})
    events = pd.DataFrame(
        {
            "name": ["demo", "demo"],
            "state_primary": ["看多", "看多"],
            "event_time": [
                dates[0] + pd.Timedelta(hours=10),
                dates[1] + pd.Timedelta(hours=10),
            ],
            "event_date": dates[:2],
            "regime": ["range", "range"],
        }
    )
    result = evaluate_intraday_mechanisms(
        events,
        pd.DataFrame(fifteen_rows),
        pd.DataFrame(five_rows),
        pd.Series("range", index=dates),
        [
            {
                "hypothesis_id": "M01",
                "mechanism": "demo",
                "signal_name": "demo",
                "state_primary": "看多",
            }
        ],
        evaluation_start=dates[0],
        exit_clocks=["09:45"],
        baseline_one_way_cost=0.0,
        stress_one_way_cost=0.0,
        density_window_sessions=2,
        median_episodes_required=1,
        p10_episodes_required=1,
        matched_random_simulations=10,
        random_seed=1,
    )

    assert len(result.episodes) == 2
    assert (result.episodes["entry_time"] > result.episodes["signal_time"]).all()
    assert (result.episodes["exit_date"] > result.episodes["entry_date"]).all()
    assert result.episodes.iloc[0]["entry_price"] == 1.001
    assert result.episodes.iloc[0]["exit_price"] == 1.01
