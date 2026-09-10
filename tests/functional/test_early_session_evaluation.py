from __future__ import annotations

import pandas as pd

from czsc_trader.early_session_evaluation import evaluate_early_session_events


def _bars() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    clocks = ("09:35", "09:40", "10:30", "10:35", "11:30", "13:05", "15:00")
    for day_number, day in enumerate(pd.date_range("2025-01-02", periods=4, freq="B")):
        direction = 1 if day_number % 2 == 0 else -1
        opening = 100.0
        for clock in clocks:
            close = opening + (1.0 * direction if clock == "11:30" else 0.0)
            rows.append(
                {
                    "Date": pd.Timestamp(f"{day.date()} {clock}"),
                    "Open": opening,
                    "Close": close,
                }
            )
    return pd.DataFrame(rows)


def test_early_session_evaluation_separates_direction_from_timing() -> None:
    days = pd.date_range("2025-01-02", periods=4, freq="B")
    events = pd.DataFrame(
        {
            "mechanism_id": "M1",
            "trade_date": days,
            "direction": [1, -1, 1, -1],
            "planned_entry": "OPEN",
            "planned_exit": "11:30_CLOSE",
        }
    )
    result = evaluate_early_session_events(
        events,
        _bars(),
        [
            {
                "mechanism_id": "M1",
                "entry": "OPEN",
                "exit": "11:30_CLOSE",
                "unconditional_opponent": "ALL",
            }
        ],
        pd.DataFrame({"segment_id": ["ALL"], "baseline_mean_return": [0.0]}),
        evaluation_start="2025-01-02",
        baseline_one_way_cost=0.0,
        stress_one_way_cost=0.0,
        base_fraction=0.5,
        positive_years_required=1,
    )
    metrics = result.metrics.set_index("variant")
    assert metrics.loc["PRIMARY", "evidence"] == "FEASIBLE_SIGNED"
    assert metrics.loc["OPPOSITE", "evidence"] == "CONTROL"
    assert metrics.loc["FIXED_LONG", "evidence"] == "TIMING_FAIL"
