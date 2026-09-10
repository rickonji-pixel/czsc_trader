from __future__ import annotations

import pandas as pd

from czsc_trader.intraday_opening_execution import evaluate_opening_shock_directions


def test_opening_shock_direction_competition_uses_identical_daily_events() -> None:
    days = pd.to_datetime(["2026-01-05", "2026-01-06"])
    events = pd.DataFrame(
        {
            "trade_date": days,
            "event_time": days + pd.Timedelta(hours=9, minutes=45),
            "shock_side": ["down", "up"],
            "opening_return": [-0.01, 0.01],
        }
    )
    bars = []
    for day, entry, exit_price in zip(days, [100.0, 100.0], [101.0, 99.0], strict=True):
        bars.extend(
            [
                {"Date": day + pd.Timedelta(hours=9, minutes=50), "Open": entry, "Close": entry},
                {"Date": day + pd.Timedelta(hours=10, minutes=30), "Open": exit_price, "Close": exit_price},
                {"Date": day + pd.Timedelta(hours=15), "Open": exit_price, "Close": exit_price},
            ]
        )
    regimes = pd.Series(["range", "trend_down"], index=days)
    result = evaluate_opening_shock_directions(
        events,
        pd.DataFrame(bars),
        regimes,
        [
            {"mechanism_id": "REVERSAL", "down_shock_direction": 1, "up_shock_direction": -1},
            {"mechanism_id": "CONTINUATION", "down_shock_direction": -1, "up_shock_direction": 1},
        ],
        evaluation_start="2026-01-05",
        exit_clocks=["10:30"],
        baseline_one_way_cost=0.0,
        stress_one_way_cost=0.0,
        base_fraction=0.5,
        density_window_sessions=1,
        median_episodes_required=1,
        p10_episodes_required=1,
        positive_years_required=1,
    )

    metrics = result.metrics.set_index("mechanism_id")
    assert metrics.loc["REVERSAL", "evidence"] == "FEASIBLE"
    assert metrics.loc["CONTINUATION", "evidence"] == "DIRECTION_FAIL"
    assert metrics.loc["REVERSAL", "stress_mean_advantage"] > 0
    assert result.episodes.groupby(["mechanism_id", "exit_clock"]).size().eq(2).all()
    assert not result.episodes.duplicated(["mechanism_id", "exit_clock", "trade_date"]).any()
