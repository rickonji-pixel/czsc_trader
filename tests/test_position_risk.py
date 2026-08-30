from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from czsc_trader.position_risk import build_risk_features


def _daily_fixture(days: int = 30) -> pd.DataFrame:
    dates = pd.bdate_range("2021-01-04", periods=days)
    close = np.linspace(10.0, 13.0, days)
    return pd.DataFrame(
        {
            "dt": dates,
            "open": close - 0.05,
            "high": close + 0.20,
            "low": close - 0.20,
            "close": close,
            "vol": np.full(days, 1000.0),
            "amount": close * 1000.0,
        }
    )


def _intraday_fixture(daily: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for row in daily.itertuples(index=False):
        day = pd.Timestamp(row.dt)
        rows.extend(
            [
                {
                    "dt": day + pd.Timedelta(hours=10),
                    "open": float(row.open),
                    "high": float(row.high),
                    "low": float(row.low),
                    "close": float(row.close) - 0.05,
                    "vol": 750.0,
                    "amount": float(row.close) * 750.0,
                },
                {
                    "dt": day + pd.Timedelta(hours=14),
                    "open": float(row.close) - 0.05,
                    "high": float(row.high),
                    "low": float(row.low),
                    "close": float(row.close),
                    "vol": 250.0,
                    "amount": float(row.close) * 250.0,
                },
            ]
        )
    return pd.DataFrame(rows)


def test_risk_features_exclude_current_bar_from_prior_high() -> None:
    daily = _daily_fixture()
    intraday = _intraday_fixture(daily)
    signal_day = pd.Timestamp(daily.loc[20, "dt"])

    features = build_risk_features(daily, intraday)

    expected = daily.set_index("dt").loc[: daily.loc[19, "dt"], "high"].tail(20).max()
    assert features.loc[signal_day, "prior_high_20"] == pytest.approx(expected)
    assert features.loc[signal_day, "prior_high_20"] < daily.loc[20, "high"]


def test_risk_features_include_only_completed_returns_through_signal_day() -> None:
    daily = _daily_fixture()
    daily["close"] = 10.0 + np.array(
        [0, 1, -1, 1, -1] * 6, dtype=float
    ).cumsum()
    daily["open"] = daily["close"]
    daily["high"] = daily["close"] + 0.2
    daily["low"] = daily["close"] - 0.2
    intraday = _intraday_fixture(daily)
    signal_day = pd.Timestamp(daily.loc[20, "dt"])

    features = build_risk_features(daily, intraday)

    indexed = daily.set_index("dt")
    returns = indexed["close"].pct_change()
    assert features.loc[signal_day, "ret_1d"] == pytest.approx(
        returns.loc[signal_day]
    )
    assert features.loc[signal_day, "sma_5"] == pytest.approx(
        indexed.loc[:signal_day, "close"].tail(5).mean()
    )
    assert features.loc[signal_day, "negative_count_5"] == pytest.approx(
        float((returns.loc[:signal_day].tail(5) < 0).sum())
    )


def test_intraday_pressure_uses_completed_same_day_bars() -> None:
    daily = _daily_fixture()
    signal_day = pd.Timestamp(daily.loc[20, "dt"])
    daily.loc[20, ["open", "high", "low", "close"]] = [9.8, 10.0, 9.0, 9.2]
    intraday = _intraday_fixture(daily)
    day_mask = intraday["dt"].dt.normalize().eq(signal_day)
    intraday.loc[day_mask, ["open", "close", "vol"]] = [
        [10.0, 9.0, 750.0],
        [9.0, 9.2, 250.0],
    ]

    features = build_risk_features(daily, intraday)
    original = features.loc[signal_day, ["close_location", "down_volume_share"]]
    mutated_intraday = intraday.copy()
    future_day = pd.Timestamp(daily.loc[21, "dt"])
    future_mask = mutated_intraday["dt"].dt.normalize().eq(future_day)
    mutated_intraday.loc[future_mask, ["open", "close", "vol"]] = [
        [20.0, 1.0, 1_000_000.0],
        [20.0, 1.0, 1_000_000.0],
    ]
    mutated_intraday.loc[future_mask, "high"] = 20.0
    mutated_intraday.loc[future_mask, "low"] = 1.0
    mutated = build_risk_features(daily, mutated_intraday)

    assert original["close_location"] == pytest.approx(0.20)
    assert original["down_volume_share"] == pytest.approx(0.75)
    pd.testing.assert_series_equal(original, mutated.loc[signal_day, original.index])


def test_risk_features_define_neutral_zero_range_and_zero_volume() -> None:
    daily = _daily_fixture()
    signal_day = pd.Timestamp(daily.loc[20, "dt"])
    daily.loc[20, ["open", "high", "low", "close"]] = [10.0, 10.0, 10.0, 10.0]
    intraday = _intraday_fixture(daily)
    day_mask = intraday["dt"].dt.normalize().eq(signal_day)
    intraday.loc[day_mask, ["open", "high", "low", "close", "vol"]] = [
        [10.0, 10.0, 10.0, 10.0, 0.0],
        [10.0, 10.0, 10.0, 10.0, 0.0],
    ]

    features = build_risk_features(daily, intraday)

    assert features.loc[signal_day, "close_location"] == pytest.approx(0.5)
    assert features.loc[signal_day, "down_volume_share"] == pytest.approx(0.0)


@pytest.mark.parametrize("problem", ["duplicate_dt", "nonpositive_close"])
def test_risk_features_reject_invalid_daily_data(problem: str) -> None:
    daily = _daily_fixture()
    if problem == "duplicate_dt":
        daily.loc[1, "dt"] = daily.loc[0, "dt"]
    else:
        daily.loc[1, "close"] = 0.0

    with pytest.raises(ValueError):
        build_risk_features(daily, _intraday_fixture(_daily_fixture()))
