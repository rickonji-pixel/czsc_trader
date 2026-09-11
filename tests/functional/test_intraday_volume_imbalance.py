from __future__ import annotations

import pandas as pd

from czsc_trader.intraday_volume_imbalance import census_first_hour_volume_imbalance


def _bars() -> pd.DataFrame:
    rows = []
    for day_number, day in enumerate(pd.date_range("2026-01-05", periods=6, freq="B")):
        for minute in range(3):
            opening = 100.0 + day_number
            direction = 1 if (day_number + minute) % 2 == 0 else -1
            rows.append(
                {
                    "Date": day + pd.Timedelta(hours=9, minutes=31 + minute),
                    "Open": opening,
                    "Close": opening + direction * (minute + 1) * 0.1,
                    "Volume": 10.0 + day_number * (minute + 1),
                }
            )
    return pd.DataFrame(rows)


def _run(frame: pd.DataFrame):
    return census_first_hour_volume_imbalance(
        frame,
        evaluation_start="2026-01-05",
        observation_start_clock="09:31",
        observation_end_clock="09:33",
        expected_bars=3,
        threshold_lookback_sessions=2,
        threshold_quantile=0.5,
        threshold_lag_sessions=1,
        density_window_sessions=2,
        median_events_required=1,
        p10_events_required=0,
        distinct_event_days_required=1,
    )


def test_volume_imbalance_census_is_causal_and_one_event_per_day() -> None:
    source = _bars()
    result = _run(source)
    assert result.features["event_time"].dt.strftime("%H:%M").eq("09:33").all()
    assert not result.events["trade_date"].duplicated().any()
    assert result.density["distinct_event_days"] == len(result.events)
    assert result.features["signed_volume_imbalance"].between(-1, 1).all()

    mutated = source.copy()
    mutated.loc[mutated["Date"] >= pd.Timestamp("2026-01-09"), "Volume"] *= 100
    rerun = _run(mutated)
    left = result.features.loc[result.features["trade_date"] < pd.Timestamp("2026-01-09")]
    right = rerun.features.loc[rerun.features["trade_date"] < pd.Timestamp("2026-01-09")]
    pd.testing.assert_frame_equal(left.reset_index(drop=True), right.reset_index(drop=True))
