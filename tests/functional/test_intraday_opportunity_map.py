from __future__ import annotations

import pandas as pd

from czsc_trader.intraday_opportunity_map import build_intraday_opportunity_map


def _bars() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    clocks = ("09:35", "09:40", "10:30", "10:35", "11:30", "13:05", "15:00")
    for day_number, day in enumerate(pd.date_range("2025-01-02", periods=3, freq="B")):
        for bar_number, clock in enumerate(clocks):
            price = 100.0 + day_number * 10.0 + bar_number
            rows.append(
                {
                    "Date": pd.Timestamp(f"{day.date()} {clock}"),
                    "Open": price,
                    "Close": price + 0.5,
                }
            )
    return pd.DataFrame(rows)


def test_opportunity_map_uses_exact_checkpoints_and_no_day_selection() -> None:
    result = build_intraday_opportunity_map(
        _bars(),
        [
            {"segment_id": "OVERNIGHT", "entry": "PREVIOUS_CLOSE", "exit": "OPEN"},
            {"segment_id": "MORNING", "entry": "OPEN", "exit": "10:30_CLOSE"},
        ],
        evaluation_start="2025-01-02",
        baseline_one_way_cost=0.0,
        stress_one_way_cost=0.001,
        base_fraction=0.5,
        account_segment_ids=["MORNING"],
    )

    overnight = result.episodes.loc[result.episodes["segment_id"].eq("OVERNIGHT")]
    morning = result.episodes.loc[result.episodes["segment_id"].eq("MORNING")]
    assert len(overnight) == 2
    assert len(morning) == 3
    assert morning.iloc[0]["entry_price"] == 100.0
    assert morning.iloc[0]["exit_price"] == 102.5
    assert set(result.metrics["segment_id"]) == {"OVERNIGHT", "MORNING"}
    assert set(result.account["cost_label"]) == {"BASELINE", "STRESS"}
