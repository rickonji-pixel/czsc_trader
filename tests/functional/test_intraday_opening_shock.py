from __future__ import annotations

import pandas as pd

from czsc_trader.intraday_opening_shock import census_opening_shocks


def _bars() -> pd.DataFrame:
    rows = []
    closes = [100.0, 101.0, 99.0, 103.0, 98.0, 104.0]
    openings = [100.0, 100.5, 99.0, 102.0, 97.0, 105.0]
    for number, day in enumerate(pd.date_range("2026-01-05", periods=6, freq="B")):
        rows.extend(
            [
                {
                    "Date": day + pd.Timedelta(hours=9, minutes=45),
                    "Close": openings[number],
                    "Volume": 10 + number,
                },
                {
                    "Date": day + pd.Timedelta(hours=15),
                    "Close": closes[number],
                    "Volume": 20 + number,
                },
            ]
        )
    return pd.DataFrame(rows)


def _run(frame: pd.DataFrame):
    return census_opening_shocks(
        frame,
        evaluation_start="2026-01-05",
        threshold_lookback_sessions=2,
        threshold_quantile=0.5,
        threshold_lag_sessions=1,
        volume_ratio_lookback_sessions=2,
        density_window_sessions=2,
        median_events_required=1,
        p10_events_required=0,
        distinct_event_days_required=1,
    )


def test_opening_shock_census_is_daily_causal_and_density_auditable() -> None:
    source = _bars()
    result = _run(source)

    assert result.features["event_time"].dt.strftime("%H:%M").eq("09:45").all()
    assert not result.events["trade_date"].duplicated().any()
    assert result.density["rolling_window_sessions"] == 2
    assert result.density["distinct_event_days"] == len(result.events)
    assert {"opening_return", "shock_threshold", "opening_volume_ratio"}.issubset(
        result.events.columns
    )

    mutated = source.copy()
    mutated.loc[mutated["Date"] >= pd.Timestamp("2026-01-09"), "Close"] *= 10
    rerun = _run(mutated)
    left = result.features.loc[result.features["trade_date"] < pd.Timestamp("2026-01-09")]
    right = rerun.features.loc[rerun.features["trade_date"] < pd.Timestamp("2026-01-09")]
    pd.testing.assert_frame_equal(left.reset_index(drop=True), right.reset_index(drop=True))
