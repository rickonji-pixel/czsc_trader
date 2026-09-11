from __future__ import annotations

import pandas as pd

from czsc_trader.early_session_events import census_early_session_events


def _bars() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    clocks = ("09:35", "09:40", "10:30", "10:35", "11:30", "13:05", "15:00")
    for day_number, day in enumerate(pd.date_range("2025-01-02", periods=12, freq="B")):
        opening = 100.0 + day_number
        for bar_number, clock in enumerate(clocks):
            price = opening + bar_number * ((-1) ** day_number) * 0.1
            rows.append(
                {
                    "Date": pd.Timestamp(f"{day.date()} {clock}"),
                    "Open": price,
                    "Close": price + ((-1) ** day_number) * 0.05,
                }
            )
    return pd.DataFrame(rows)


def _mechanisms() -> list[dict[str, str]]:
    return [
        {
            "mechanism_id": "PRIOR_INTRADAY_CONTINUATION",
            "information_clock": "PREVIOUS_15:00",
            "planned_entry": "OPEN",
            "planned_exit": "11:30_CLOSE",
        },
        {
            "mechanism_id": "LATE_SESSION_FLOW_CONTINUATION",
            "information_clock": "PREVIOUS_15:00",
            "planned_entry": "OPEN",
            "planned_exit": "11:30_CLOSE",
        },
        {
            "mechanism_id": "OPENING_GAP_REVERSION",
            "information_clock": "09:30",
            "planned_entry": "09:40_OPEN",
            "planned_exit": "11:30_CLOSE",
        },
    ]


def test_early_session_census_is_lagged_and_keeps_mechanisms_separate() -> None:
    result = census_early_session_events(
        _bars(),
        _mechanisms(),
        evaluation_start="2025-01-02",
        threshold_lookback_sessions=3,
        threshold_quantile=0.75,
        threshold_lag_sessions=1,
        density_window_sessions=3,
        target_median_min=1,
        target_median_max=3,
        median_events_required=1,
        p10_events_required=0,
    )
    assert set(result.density["mechanism_id"]) == {
        "PRIOR_INTRADAY_CONTINUATION",
        "LATE_SESSION_FLOW_CONTINUATION",
        "OPENING_GAP_REVERSION",
    }
    assert not result.events.duplicated(["mechanism_id", "trade_date"]).any()
    assert result.events["direction"].isin([-1, 1]).all()
    first_event = result.events.groupby("mechanism_id")["trade_date"].min()
    assert first_event.ge(pd.Timestamp("2025-01-08")).all()
