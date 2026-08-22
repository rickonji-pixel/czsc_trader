from pathlib import Path

import pandas as pd

from czsc_trader.data import load_market_data


RAW_DIR = Path("data/raw")


def test_loads_verified_market_data() -> None:
    """Catch missing partitions, normalization, or intraday sessions."""
    data = load_market_data(RAW_DIR)

    assert len(data.intraday) == 5_112
    assert len(data.daily) == 639
    assert len(data.weekly) == 136
    assert data.intraday["symbol"].unique().tolist() == ["588080.SH"]
    assert data.intraday.groupby(data.intraday["dt"].dt.normalize()).size().eq(8).all()
    assert len(data.hashes) == 9


def test_cross_frequency_prices_reconcile() -> None:
    """Catch incorrect year concatenation or OHLC aggregation."""
    data = load_market_data(RAW_DIR)
    intraday = data.intraday.assign(date=data.intraday["dt"].dt.normalize())
    aggregated = intraday.groupby("date").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    )
    daily = data.daily.set_index("dt")

    pd.testing.assert_frame_equal(
        aggregated,
        daily[["open", "high", "low", "close"]],
        check_freq=False,
        check_names=False,
    )


def test_truncate_keeps_only_information_available_by_cutoff() -> None:
    """Catch future rows leaking through a truncated research view."""
    data = load_market_data(RAW_DIR)
    cutoff = pd.Timestamp("2025-06-30")
    truncated = data.truncate(cutoff)

    assert truncated.intraday["dt"].max().normalize() <= cutoff
    assert truncated.daily["dt"].max() <= cutoff
    assert truncated.weekly["dt"].max() <= cutoff
    assert truncated.hashes == data.hashes
