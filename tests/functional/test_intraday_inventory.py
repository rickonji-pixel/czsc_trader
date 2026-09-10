from __future__ import annotations

import pandas as pd

from czsc_trader.intraday_inventory import evaluate_inventory_mechanisms


def test_inventory_roll_closes_same_day_and_adds_cash_alpha():
    dates = pd.to_datetime(["2026-01-05", "2026-01-06"])
    five_rows = []
    fifteen_rows = []
    for day in dates:
        five_rows.extend(
            [
                {"Date": day + pd.Timedelta(hours=10, minutes=5), "Open": 1.0, "Close": 1.0},
                {"Date": day + pd.Timedelta(hours=10, minutes=10), "Open": 1.0, "Close": 1.01},
                {"Date": day + pd.Timedelta(hours=10, minutes=15), "Open": 1.01, "Close": 1.03},
                {"Date": day + pd.Timedelta(hours=15), "Open": 1.03, "Close": 1.03},
            ]
        )
        fifteen_rows.append({"Date": day + pd.Timedelta(hours=10)})
    events = pd.DataFrame(
        {
            "name": ["demo", "demo"],
            "state_primary": ["看多", "看多"],
            "event_time": [day + pd.Timedelta(hours=10) for day in dates],
            "event_date": dates,
            "regime": ["range", "range"],
        }
    )
    result = evaluate_inventory_mechanisms(
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
        exit_bars=[3],
        baseline_one_way_cost=0.0,
        stress_one_way_cost=0.0,
        base_fraction=0.5,
        density_window_sessions=1,
        median_episodes_required=1,
        p10_episodes_required=1,
        matched_random_simulations=10,
        random_seed=1,
    )

    assert len(result.episodes) == 2
    assert (result.episodes["entry_date"] == result.episodes["exit_date"]).all()
    assert (result.episodes["stress_return"].round(6) == 0.03).all()
    assert result.account.iloc[0]["incremental_terminal_return_vs_static"] > 0
