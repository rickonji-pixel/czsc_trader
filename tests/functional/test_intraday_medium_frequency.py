from __future__ import annotations

import pandas as pd

from czsc_trader.intraday_medium_frequency import census_medium_frequency_mechanisms


def _bars() -> pd.DataFrame:
    rows = []
    for day_number, day in enumerate(pd.date_range("2025-01-02", periods=12, freq="B")):
        for minute in range(3):
            opening = 100.0 + day_number * 0.1 + minute * 0.05
            close = opening + ((-1) ** (day_number + minute)) * (0.02 + day_number * 0.005)
            rows.append(
                {
                    "Date": day + pd.Timedelta(hours=9, minutes=31 + minute),
                    "Open": opening,
                    "High": max(opening, close) + 0.01,
                    "Low": min(opening, close) - 0.01,
                    "Close": close,
                    "Volume": 100.0 + day_number * 10 + minute,
                }
            )
    return pd.DataFrame(rows)


def _run(frame: pd.DataFrame):
    return census_medium_frequency_mechanisms(
        frame,
        evaluation_start="2025-01-02",
        observation_start_clock="09:31",
        observation_end_clock="09:33",
        expected_bars=3,
        threshold_lookback_sessions=3,
        threshold_quantile=0.75,
        threshold_lag_sessions=1,
        activity_baseline_sessions=2,
        density_window_sessions=3,
        target_median_min=1,
        target_median_max=3,
        median_events_required=1,
        p10_events_required=0,
        distinct_event_days_required=1,
    )


def test_medium_frequency_census_keeps_mechanisms_causal_and_separate() -> None:
    source = _bars()
    result = _run(source)
    assert set(result.density["mechanism_id"]) == {
        "VWAP_DEVIATION_REVERSION",
        "DIRECTIONAL_EFFICIENCY_CONTINUATION",
        "ABNORMAL_ACTIVITY_CONTINUATION",
    }
    assert not result.events.duplicated(["mechanism_id", "trade_date"]).any()
    assert result.events["direction"].isin([-1, 1]).all()
    assert result.features["event_time"].dt.strftime("%H:%M").eq("09:33").all()

    mutated = source.copy()
    mutated.loc[mutated["Date"] >= pd.Timestamp("2025-01-13"), "Close"] *= 1.2
    rerun = _run(mutated)
    left = result.features.loc[result.features["trade_date"] < pd.Timestamp("2025-01-13")]
    right = rerun.features.loc[rerun.features["trade_date"] < pd.Timestamp("2025-01-13")]
    pd.testing.assert_frame_equal(left.reset_index(drop=True), right.reset_index(drop=True))
