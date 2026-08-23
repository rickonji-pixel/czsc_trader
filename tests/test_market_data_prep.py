from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pandas as pd
import pytest

from czsc_trader.data import load_market_data
from czsc_trader.market_data_prep import prepare_market_data, validate_market_frames


SESSION_TIMES = ("10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00")


def _valid_frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, object]] = []
    for day, base in (("2026-08-20", 100.0), ("2026-08-21", 102.0)):
        for offset, close_time in enumerate(SESSION_TIMES):
            open_ = base + offset * 0.1
            close = open_ + 0.05
            rows.append(
                {
                    "Date": f"{day} {close_time}:00",
                    "Open": open_,
                    "High": close + 0.05,
                    "Low": open_ - 0.05,
                    "Close": close,
                    "Volume": 1000.0 + offset,
                    "Amount": (1000.0 + offset) * close,
                }
            )
    intraday = pd.DataFrame(rows)
    timestamp = pd.to_datetime(intraday["Date"])
    daily = (
        intraday.assign(_day=timestamp.dt.normalize())
        .groupby("_day", sort=True)
        .agg(
            Open=("Open", "first"),
            High=("High", "max"),
            Low=("Low", "min"),
            Close=("Close", "last"),
            Volume=("Volume", "sum"),
            Amount=("Amount", "sum"),
        )
        .reset_index(names="Date")
    )
    daily["Date"] = daily["Date"].dt.strftime("%Y-%m-%d")
    weekly = pd.DataFrame(
        {
            "Date": ["2026-08-21"],
            "Open": [daily.iloc[0]["Open"]],
            "High": [daily["High"].max()],
            "Low": [daily["Low"].min()],
            "Close": [daily.iloc[-1]["Close"]],
            "Volume": [daily["Volume"].sum()],
            "Amount": [daily["Amount"].sum()],
        }
    )
    return intraday, daily, weekly


def test_validate_market_frames_accepts_complete_reconciled_data() -> None:
    intraday, daily, weekly = _valid_frames()

    result = validate_market_frames(intraday, daily, weekly)

    assert result["status"] == "PASS"
    assert result["intraday"]["complete_day_count"] == 2
    assert result["reconciliation"]["daily_matched_days"] == 2


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda frame: pd.concat([frame, frame.iloc[[0]]], ignore_index=True), "duplicate"),
        (lambda frame: frame.iloc[1:].reset_index(drop=True), "eight bars"),
        (lambda frame: frame.assign(High=lambda item: item["Low"] - 1), "OHLC"),
    ],
)
def test_validate_market_frames_rejects_bad_intraday(mutation, message: str) -> None:
    intraday, daily, weekly = _valid_frames()

    with pytest.raises(ValueError, match=message):
        validate_market_frames(mutation(intraday), daily, weekly)


def test_validate_market_frames_rejects_daily_mismatch() -> None:
    intraday, daily, weekly = _valid_frames()
    daily.loc[0, "Close"] += 0.02

    with pytest.raises(ValueError, match="30m/daily reconciliation"):
        validate_market_frames(intraday, daily, weekly)


def test_validate_market_frames_accepts_price_difference_at_exact_tolerance() -> None:
    intraday, daily, weekly = _valid_frames()
    intraday.loc[0, ["Open", "Low"]] = [1.031, 1.0]
    daily.loc[0, ["Open", "Low"]] = [1.036, 1.0]
    weekly.loc[0, ["Open", "Low"]] = [1.036, 1.0]

    result = validate_market_frames(intraday, daily, weekly)

    assert result["status"] == "PASS"


def test_validate_market_frames_rejects_weekly_mismatch() -> None:
    intraday, daily, weekly = _valid_frames()
    weekly.loc[0, "Close"] += 0.02

    with pytest.raises(ValueError, match="daily/weekly reconciliation"):
        validate_market_frames(intraday, daily, weekly)


def _fake_fetcher(
    symbol: str,
    asset_type: str,
    start: date,
    end: date,
    period: str,
) -> tuple[pd.DataFrame, dict[str, str]]:
    frames = dict(zip(("30m", "daily", "weekly"), _valid_frames(), strict=True))
    return frames[period].copy(), {
        "vendor": "tushare",
        "market": "a_share",
        "vendor_symbol": symbol,
        "asset_type": asset_type,
        "period": period,
    }


def test_prepare_splits_years_and_writes_pass_manifest(tmp_path: Path) -> None:
    result = prepare_market_data(
        "600519.SH",
        "stock",
        date(2026, 8, 20),
        date(2026, 8, 21),
        tmp_path,
        fetcher=_fake_fetcher,
    )

    assert result["validation_status"] == "PASS"
    assert (tmp_path / "600519_30m_2026.csv").is_file()
    manifest = json.loads((tmp_path / "600519_manifest.json").read_text(encoding="utf-8"))
    assert manifest["symbol"] == "600519.SH"
    assert manifest["asset_type"] == "stock"
    assert manifest["files"]["600519_30m_2026.csv"]["sha256"]
    validation = json.loads((tmp_path / "600519_validation.json").read_text(encoding="utf-8"))
    assert validation["status"] == "PASS"


def test_prepared_data_at_price_tolerance_can_be_loaded(tmp_path: Path) -> None:
    intraday, daily, weekly = _valid_frames()
    intraday.loc[0, ["Open", "Low"]] = [1.031, 1.0]
    daily.loc[0, ["Open", "Low"]] = [1.036, 1.0]
    weekly.loc[0, ["Open", "Low"]] = [1.036, 1.0]
    frames = {"30m": intraday, "daily": daily, "weekly": weekly}

    def boundary_fetcher(symbol, asset_type, start, end, period):
        return frames[period].copy(), {
            "vendor": "tushare",
            "market": "a_share",
            "vendor_symbol": symbol,
            "asset_type": asset_type,
            "period": period,
        }

    prepare_market_data(
        "600519.SH",
        "stock",
        date(2026, 8, 20),
        date(2026, 8, 21),
        tmp_path,
        fetcher=boundary_fetcher,
    )

    loaded = load_market_data(tmp_path, "600519.SH", "stock")

    assert len(loaded.daily) == 2


def test_failed_validation_does_not_replace_existing_files(tmp_path: Path) -> None:
    existing = tmp_path / "600519_daily_2026.csv"
    existing.write_bytes(b"old")

    def bad_fetcher(*args, **kwargs):
        frame, metadata = _fake_fetcher(*args, **kwargs)
        if args[-1] == "daily":
            frame.loc[0, "Close"] += 0.02
        return frame, metadata

    with pytest.raises(ValueError, match="reconciliation"):
        prepare_market_data(
            "600519.SH",
            "stock",
            date(2026, 8, 20),
            date(2026, 8, 21),
            tmp_path,
            fetcher=bad_fetcher,
        )

    assert existing.read_bytes() == b"old"


def test_prepare_requires_explicit_supported_symbol_and_asset(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="stock or etf"):
        prepare_market_data("600519.SH", "auto", date(2026, 1, 1), date(2026, 1, 2), tmp_path)
    with pytest.raises(ValueError, match="SH or SZ"):
        prepare_market_data("00700.HK", "stock", date(2026, 1, 1), date(2026, 1, 2), tmp_path)
