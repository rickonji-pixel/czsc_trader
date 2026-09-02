from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pandas as pd
import pytest

from czsc_trader.identity import raw_file_sha256


def _vendor_frame(rows: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Date": timestamp,
                "Open": close,
                "High": close,
                "Low": close,
                "Close": close,
                "Volume": 1000.0,
                "Amount": close * 1000.0,
            }
            for timestamp, close in rows
        ]
    )


def test_prepare_market_data_publishes_unadjusted_execution_prices(tmp_path: Path) -> None:
    from czsc_trader.data import load_execution_prices
    from czsc_trader.market_data_prep import prepare_market_data

    day = "2026-09-01"
    intraday = _vendor_frame(
        [(f"{day} {time}:00", 1.704) for time in ("10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00")]
    )
    intraday["Volume"] = 125.0
    intraday["Amount"] = 213.0
    adjusted = {
        "30m": intraday,
        "daily": _vendor_frame([(day, 1.704)]),
        "weekly": _vendor_frame([(day, 1.704)]),
    }
    adjusted["daily"]["Volume"] = 1000.0
    adjusted["daily"]["Amount"] = 1704.0
    adjusted["weekly"]["Volume"] = 1000.0
    adjusted["weekly"]["Amount"] = 1704.0

    def fetcher(symbol: str, asset: str, start: date, end: date, period: str):
        return adjusted[period], {
            "vendor": "tushare",
            "vendor_symbol": symbol,
            "asset_type": asset,
            "period": period,
            "adjustment": "hfq",
            "adjustment_factor_source": "fund_adj",
            "adjustment_factor_sha256": "factor-hash",
        }

    def execution_fetcher(symbol: str, asset: str, start: date, end: date):
        return _vendor_frame([(day, 1.688)]), {
            "vendor": "tushare",
            "vendor_symbol": symbol,
            "asset_type": asset,
            "period": "daily",
            "adjustment": "none",
        }

    result = prepare_market_data(
        "588080.SH",
        "etf",
        date(2026, 9, 1),
        date(2026, 9, 1),
        tmp_path,
        fetcher=fetcher,
        execution_fetcher=execution_fetcher,
        name_fetcher=lambda _symbol, _asset: "科创50ETF",
    )

    assert result["execution_price_manifest"].endswith("588080_execution_manifest.json")
    prices = load_execution_prices(tmp_path, "588080.SH", "etf")
    assert prices.iloc[-1]["dt"] == pd.Timestamp(day)
    assert prices.iloc[-1]["close"] == pytest.approx(1.688)


def test_load_execution_prices_rejects_tampered_file(tmp_path: Path) -> None:
    from czsc_trader.data import load_execution_prices

    price_path = tmp_path / "588080_execution_daily_2026.csv"
    price_path.write_text(
        "date,open,high,low,close,volume,amount\n2026-09-01,1.688,1.688,1.688,1.688,1000,1688\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "symbol": "588080.SH",
        "asset_type": "etf",
        "vendor": "tushare",
        "adjustment": "none",
        "files": {
            price_path.name: {
                "frequency": "daily",
                "year": 2026,
                "sha256": raw_file_sha256(price_path),
            }
        },
    }
    (tmp_path / "588080_execution_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    price_path.write_text(price_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="SHA-256 differs"):
        load_execution_prices(tmp_path, "588080.SH", "etf")
