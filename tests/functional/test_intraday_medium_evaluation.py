from __future__ import annotations

import pandas as pd

from czsc_trader.intraday_medium_evaluation import evaluate_medium_frequency_mechanisms


def test_medium_frequency_evaluation_compares_exact_opposite_directions() -> None:
    days = pd.to_datetime(["2026-01-05", "2026-01-06"])
    events = pd.DataFrame(
        {
            "mechanism_id": ["M1", "M1"],
            "trade_date": days,
            "event_time": days + pd.Timedelta(hours=10, minutes=30),
            "direction": [1, -1],
        }
    )
    bars = []
    for day, end in zip(days, [101.0, 99.0], strict=True):
        bars.extend(
            [
                {"Date": day + pd.Timedelta(hours=10, minutes=35), "Open": 100.0, "Close": 100.0},
                {"Date": day + pd.Timedelta(hours=15), "Open": end, "Close": end},
            ]
        )
    result = evaluate_medium_frequency_mechanisms(
        events,
        pd.DataFrame(bars),
        pd.Series(["range", "range"], index=days),
        ["M1"],
        evaluation_start="2026-01-05",
        entry_clock="10:35",
        exit_clock="15:00",
        baseline_one_way_cost=0.0,
        stress_one_way_cost=0.0,
        base_fraction=0.5,
        density_window_sessions=1,
        median_episodes_required=1,
        p10_episodes_required=1,
        positive_years_required=1,
    )
    metrics = result.metrics.set_index("variant")
    assert metrics.loc["PRIMARY", "evidence"] == "FEASIBLE"
    assert metrics.loc["PRIMARY", "stress_mean_advantage"] > 0
    assert metrics.loc["OPPOSITE", "evidence"] == "CONTROL"
    primary = result.episodes.loc[result.episodes["variant"].eq("PRIMARY")]
    opposite = result.episodes.loc[result.episodes["variant"].eq("OPPOSITE")]
    assert primary["executed_direction"].tolist() == [-item for item in opposite["executed_direction"]]
