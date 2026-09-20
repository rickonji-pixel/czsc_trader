from __future__ import annotations

import pandas as pd
import pytest

from dataflows import DataRepairError
from dataflows.history_repair import (
    RepairBinding,
    SeriesKey,
    apply_repairs_once,
    inspect_registered_source_anomalies,
    rebuild_intraday_from_1m,
    validate_repair_registry,
)
from dataflows.history_validation import (
    ValidationFinding,
    inspect_intraday_against_daily,
    inspect_market_collection,
)


def _daily(trade_dates: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Date": trade_dates,
            "Open": 10.0,
            "High": 10.0,
            "Low": 10.0,
            "Close": 10.0,
            "Volume": 240.0,
            "Amount": 2400.0,
        }
    )


def _one_minute_day(trade_date: str) -> pd.DataFrame:
    day = pd.Timestamp(trade_date)
    regular = [
        *pd.date_range(day + pd.Timedelta(hours=9, minutes=31), periods=120, freq="1min"),
        *pd.date_range(day + pd.Timedelta(hours=13, minutes=1), periods=120, freq="1min"),
    ]
    timestamps = [day + pd.Timedelta(hours=9, minutes=30), *regular]
    frame = pd.DataFrame(
        {
            "Date": timestamps,
            "Open": 10.0,
            "High": 10.0,
            "Low": 10.0,
            "Close": 10.0,
            "Volume": [0.0, *([1.0] * 240)],
            "Amount": [0.0, *([10.0] * 240)],
        }
    )
    frame.loc[0, ["Open", "High", "Low", "Close"]] = 99.0
    return frame


def test_known_source_anomaly_fails_before_exact_repair() -> None:
    frame = pd.DataFrame(
        [
            {
                "Date": "2020-03-09",
                "Open": 3.716,
                "High": 3.728,
                "Low": 3.317,
                "Close": 3.659,
                "Volume": 711691432.0,
                "Amount": 2624195457.0,
            }
        ]
    )
    series = SeriesKey(
        "tushare", "fund_daily", "518880.SH", "etf.ohlcv", "daily", "none"
    )

    findings = inspect_registered_source_anomalies(frame, series)
    repaired, records = apply_repairs_once(frame, series, findings)

    assert [item.code for item in findings] == ["KNOWN_SOURCE_ANOMALY"]
    assert repaired.loc[0, "Low"] == 3.548
    assert len(records) == 1
    assert records[0].binding_id == "TUSHARE_518880_DAILY_FIELDS_V1"
    assert records[0].raw_content_sha256 != records[0].repaired_content_sha256
    assert records[0].to_dict()["affected_date_count"] == 1
    assert not inspect_registered_source_anomalies(repaired, series)


def test_unknown_source_signature_is_blocked() -> None:
    frame = pd.DataFrame(
        [
            {
                "Date": "2020-03-09",
                "Open": 3.716,
                "High": 3.728,
                "Low": 3.400,
                "Close": 3.659,
                "Volume": 711691432.0,
                "Amount": 2624195457.0,
            }
        ]
    )
    series = SeriesKey(
        "tushare", "fund_daily", "518880.SH", "etf.ohlcv", "daily", "none"
    )
    findings = inspect_registered_source_anomalies(frame, series)

    assert [item.code for item in findings] == ["SOURCE_SIGNATURE_UNKNOWN"]
    with pytest.raises(DataRepairError, match="no repair binding matched"):
        apply_repairs_once(frame, series, findings)


def test_repair_binding_does_not_cross_symbol_boundary() -> None:
    frame = pd.DataFrame(
        [
            {
                "Date": "2020-03-09",
                "Open": 3.716,
                "High": 3.728,
                "Low": 3.317,
                "Close": 3.659,
                "Volume": 1.0,
                "Amount": 1.0,
            }
        ]
    )
    other = SeriesKey(
        "tushare", "fund_daily", "518850.SH", "etf.ohlcv", "daily", "none"
    )

    assert not inspect_registered_source_anomalies(frame, other)


def test_rebuilt_intraday_is_revalidated_against_daily() -> None:
    one_minute = _one_minute_day("2024-01-02")
    daily = _daily(["2024-01-02"])

    rebuilt = rebuild_intraday_from_1m(
        one_minute, daily, "30m", dates=["2024-01-02"]
    )
    report = inspect_intraday_against_daily(rebuilt, daily, "30m")

    assert report.passed
    assert len(rebuilt) == 8
    assert rebuilt[["Open", "High", "Low", "Close"]].eq(10.0).all().all()


def test_518880_rebuild_is_limited_to_registered_dates() -> None:
    registered_date = "2015-07-16"
    ordinary_date = "2015-07-17"
    daily = _daily([registered_date, ordinary_date])
    one_minute = pd.concat(
        [_one_minute_day(registered_date), _one_minute_day(ordinary_date)],
        ignore_index=True,
    )
    frame = rebuild_intraday_from_1m(
        one_minute, daily, "30m", dates=[registered_date, ordinary_date]
    )
    frame.loc[
        pd.to_datetime(frame["Date"]).dt.strftime("%Y-%m-%d").eq(registered_date),
        "High",
    ] = 9.0
    ordinary_before = frame.loc[
        pd.to_datetime(frame["Date"]).dt.strftime("%Y-%m-%d").eq(ordinary_date)
    ].copy()
    series = SeriesKey(
        "tushare", "etf_mins", "518880.SH", "etf.ohlcv", "30m", "none"
    )
    findings = (
        ValidationFinding(
            "INVALID_OHLCV",
            "registered source anomaly",
            {"timestamps": [f"{registered_date} 10:00:00"]},
        ),
    )

    repaired, records = apply_repairs_once(
        frame,
        series,
        findings,
        references={"1m": one_minute, "daily": daily},
    )

    assert records[0].affected_dates == (registered_date,)
    pd.testing.assert_frame_equal(
        repaired.loc[
            pd.to_datetime(repaired["Date"])
            .dt.strftime("%Y-%m-%d")
            .eq(ordinary_date)
        ].reset_index(drop=True),
        ordinary_before.reset_index(drop=True),
        check_dtype=False,
    )
    assert inspect_intraday_against_daily(repaired, daily, "30m").passed


def test_518880_unknown_rebuild_date_is_blocked() -> None:
    trade_date = "2015-07-17"
    daily = _daily([trade_date])
    one_minute = _one_minute_day(trade_date)
    frame = rebuild_intraday_from_1m(one_minute, daily, "30m", dates=[trade_date])
    frame.loc[0, "High"] = 9.0
    series = SeriesKey(
        "tushare", "etf_mins", "518880.SH", "etf.ohlcv", "30m", "none"
    )
    findings = (
        ValidationFinding(
            "INVALID_OHLCV",
            "unknown source anomaly",
            {"timestamps": [f"{trade_date} 10:00:00"]},
        ),
    )

    with pytest.raises(DataRepairError, match="no repair binding matched"):
        apply_repairs_once(
            frame,
            series,
            findings,
            references={"1m": one_minute, "daily": daily},
        )


def test_repair_is_not_executed_without_a_validation_finding() -> None:
    frame = _daily(["2024-01-02"])
    series = SeriesKey(
        "tushare", "fund_daily", "518880.SH", "etf.ohlcv", "daily", "none"
    )

    unchanged, records = apply_repairs_once(frame, series, ())

    pd.testing.assert_frame_equal(unchanged, frame)
    assert records == ()


def test_requested_start_truncation_is_a_validation_failure() -> None:
    daily = _daily(["2024-01-03"])
    calendar = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "IsOpen": [1, 1],
        }
    )

    report = inspect_market_collection(
        {"daily": daily},
        trading_calendar=calendar,
        expected_start="2024-01-02",
    )

    assert not report.passed
    finding = next(
        item for item in report.findings if item.code == "CALENDAR_COVERAGE_MISMATCH"
    )
    assert finding.context["missing"] == ["2024-01-02"]


def test_invalid_daily_and_empty_calendar_return_findings_instead_of_crashing() -> None:
    invalid_daily = _daily(["2024-01-02"])
    invalid_daily.loc[0, "High"] = float("inf")

    report = inspect_market_collection(
        {"daily": invalid_daily},
        trading_calendar=pd.DataFrame(columns=["Date", "IsOpen"]),
    )

    assert not report.passed
    assert [item.code for item in report.findings] == ["NON_NUMERIC_VALUE"]

    empty_calendar_report = inspect_market_collection(
        {"daily": _daily(["2024-01-02"])},
        trading_calendar=pd.DataFrame(columns=["Date", "IsOpen"]),
    )
    assert [item.code for item in empty_calendar_report.findings] == [
        "INVALID_CALENDAR"
    ]


def test_repair_registry_rejects_ambiguous_bindings() -> None:
    series = SeriesKey(
        "tushare", "etf_mins", "510500.SH", "etf.ohlcv", "15m", "none"
    )
    left = RepairBinding(
        "LEFT",
        series,
        "2024-01-01",
        "2024-01-31",
        ("TRADING_DATE_MISMATCH",),
        "REBUILD_INTRADAY_FROM_1M",
        1,
    )
    right = RepairBinding(
        "RIGHT",
        series,
        "2024-01-15",
        "2024-02-15",
        ("TRADING_DATE_MISMATCH",),
        "REBUILD_INTRADAY_FROM_1M",
        1,
    )

    with pytest.raises(DataRepairError, match="ambiguous"):
        validate_repair_registry((left, right))
