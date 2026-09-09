from __future__ import annotations

import pandas as pd

from czsc_trader.signal_census import (
    evaluate_signal_events,
    primary_value,
    semantic_value,
)


def test_signal_census_uses_transition_events_and_next_session_prices() -> None:
    dates = pd.date_range("2026-01-05", periods=6, freq="B")
    signal_id = "SIG-DAILY-TEST"
    primary = pd.DataFrame(
        {signal_id: ["其他", "买点", "买点", "其他", "买点", "其他"]}, index=dates
    )
    full = pd.DataFrame(
        {signal_id: ["其他_任意_任意", "买点_一类_任意", "买点_一类_任意", "其他_任意_任意", "买点_二类_任意", "其他_任意_任意"]},
        index=dates,
    )
    catalog = pd.DataFrame(
        [{
            "signal_id": signal_id,
            "name": "example_signal",
            "namespace": "example",
            "frequency": "daily",
            "status": "GENERATED",
            "s001_reference_function": False,
        }]
    )
    execution = pd.DataFrame(
        {
            "dt": dates,
            "open": [10.0, 11.0, 12.0, 13.0, 20.0, 21.0],
            "high": [10.5, 12.0, 13.0, 14.0, 22.0, 22.0],
            "low": [9.5, 10.0, 11.0, 12.0, 19.0, 20.0],
            "close": [10.0, 11.5, 12.5, 13.5, 21.0, 21.5],
        }
    )

    metrics, stability, events, redundancy = evaluate_signal_events(
        primary,
        full,
        catalog,
        execution,
        evaluation_start=dates[0],
        horizons=[1],
        fee_rate=0.0,
        sparse_events_below=10,
        narrow_coverage_years_below=3,
        concentrated_year_share_above=0.5,
    )

    buy_events = events.loc[events["state_primary"] == "买点"]
    assert buy_events["signal_date"].tolist() == ["2026-01-06", "2026-01-09"]
    buy_metric = metrics.loc[metrics["state_primary"] == "买点"].iloc[0]
    assert buy_metric["episode_count"] == 2
    assert buy_metric["event_count"] == 2
    assert buy_metric["raw_return_mean"] == ((12.5 / 12.0 - 1) + (21.5 / 21.0 - 1)) / 2
    assert len(stability.loc[stability["state_primary"] == "买点"]) == 1
    assert redundancy["definition"].startswith("exact equality")
    assert primary_value("买点_一类_任意_0") == "买点"
    assert semantic_value("买点_一类_任意_0") == "买点_一类_任意"
