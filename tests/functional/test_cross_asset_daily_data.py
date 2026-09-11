from __future__ import annotations

import pandas as pd
import pytest

from czsc_trader.cross_asset_daily_data import (
    DailySnapshot,
    align_adjusted_to_target_calendar,
    canonicalize_daily,
    synchronize_snapshots,
    validate_daily_pair,
)


def _daily(symbol: str, dates: list[str], factor: float = 2.0) -> tuple[pd.DataFrame, pd.DataFrame]:
    execution = pd.DataFrame(
        {
            "dt": pd.to_datetime(dates),
            "symbol": symbol,
            "open": [10.0 + index for index in range(len(dates))],
            "high": [11.0 + index for index in range(len(dates))],
            "low": [9.0 + index for index in range(len(dates))],
            "close": [10.5 + index for index in range(len(dates))],
            "vol": [100.0] * len(dates),
            "amount": [1000.0] * len(dates),
        }
    )
    adjusted = execution.copy()
    adjusted.loc[:, ["open", "high", "low", "close"]] *= factor
    adjusted["vol"] /= factor
    return adjusted, execution


def test_cross_asset_daily_snapshot_requires_exact_shared_calendar() -> None:
    symbols = ("510500.SH", "510300.SH", "512100.SH")
    snapshots = {}
    for symbol in symbols:
        adjusted, execution = _daily(symbol, ["2026-09-07", "2026-09-08"])
        snapshots[symbol] = DailySnapshot(symbol, adjusted, execution, {})

    clipped, calendar, report = synchronize_snapshots(
        snapshots, "2026-09-07", "2026-09-08"
    )

    assert set(clipped) == set(symbols)
    assert calendar["dt"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-09-07",
        "2026-09-08",
    ]
    assert report["exact_calendar_match"] is True
    assert report["common_sessions"] == 2

    adjusted, execution = _daily("512100.SH", ["2026-09-07"])
    snapshots["512100.SH"] = DailySnapshot("512100.SH", adjusted, execution, {})
    with pytest.raises(ValueError, match="trading calendars differ"):
        synchronize_snapshots(snapshots, "2026-09-07", "2026-09-08")


def test_daily_pair_rejects_inconsistent_adjustment_factor() -> None:
    adjusted, execution = _daily("510300.SH", ["2026-09-07", "2026-09-08"])
    adjusted.loc[0, "high"] += 1.0

    with pytest.raises(ValueError, match="one daily factor"):
        validate_daily_pair(adjusted, execution, "510300.SH")

    vendor = adjusted.rename(
        columns={
            "dt": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "vol": "Volume",
            "amount": "Amount",
        }
    )
    assert canonicalize_daily(vendor, "510300.SH").columns.tolist() == [
        "dt", "symbol", "open", "high", "low", "close", "vol", "amount"
    ]


def test_reference_suspension_is_causally_carried_and_audited() -> None:
    target_adjusted, target_execution = _daily(
        "510500.SH", ["2022-09-01", "2022-09-02", "2022-09-05"]
    )
    reference_adjusted, reference_execution = _daily(
        "512100.SH", ["2022-09-01", "2022-09-05"]
    )
    snapshots = {
        "510500.SH": DailySnapshot(
            "510500.SH", target_adjusted, target_execution, {}
        ),
        "512100.SH": DailySnapshot(
            "512100.SH", reference_adjusted, reference_execution, {}
        ),
    }

    panel, audit = align_adjusted_to_target_calendar(
        snapshots,
        "510500.SH",
        {"512100.SH": {"2022-09-02"}},
    )

    carried = panel.loc[
        panel["symbol"].eq("512100.SH")
        & panel["dt"].eq(pd.Timestamp("2022-09-02"))
    ].iloc[0]
    prior = panel.loc[
        panel["symbol"].eq("512100.SH")
        & panel["dt"].eq(pd.Timestamp("2022-09-01"))
    ].iloc[0]
    assert carried["observed"] == False  # noqa: E712
    assert carried["source_dt"] == pd.Timestamp("2022-09-01")
    assert carried["staleness_sessions"] == 1
    assert carried[["open", "high", "low", "close"]].nunique() == 1
    assert carried["close"] == prior["close"]
    assert carried["vol"] == 0
    assert carried["amount"] == 0
    assert audit["symbols"]["512100.SH"]["carried_dates"] == ["2022-09-02"]

    with pytest.raises(ValueError, match="unapproved"):
        align_adjusted_to_target_calendar(snapshots, "510500.SH", {})
