from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pandas as pd
import pytest

from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.us_data_service import (
    PrepareUSHistoryCommand,
    prepare_us_history,
)
from dataflows import DataRequest, Dataflows, Dataset


def _bars(frequency: str) -> pd.DataFrame:
    values = {
        "30m": [
            item.strftime("%Y-%m-%d %H:%M:%S")
            for day in ("2024-01-02", "2024-01-03")
            for item in pd.date_range(f"{day} 10:00:00", f"{day} 16:00:00", freq="30min")
        ],
        "daily": ["2024-01-02", "2024-01-03"],
        "weekly": ["2024-01-03"],
    }[frequency]
    return pd.DataFrame(
        {
            "Date": values,
            "Open": [10.0] * len(values),
            "High": [10.2] * len(values),
            "Low": [9.8] * len(values),
            "Close": [10.1] * len(values),
            "Volume": [100] * len(values),
            "Amount": [1000.0] * len(values),
        }
    )


def _dataflows() -> Dataflows:
    def adjusted(request: DataRequest):
        return _bars(request.frequency), {
            "vendor": "longbridge-test",
            "market": "us",
            "trade_sessions": "intraday",
            "period": request.frequency,
            "primary_key": ["Date"],
        }

    def execution(request: DataRequest):
        return _bars("daily"), {
            "vendor": "longbridge-test",
            "period": "daily",
            "adjustment": "none",
            "primary_key": ["Date"],
        }

    def calendar(request: DataRequest):
        dates = pd.date_range(request.start, request.end, freq="D")
        return pd.DataFrame(
            {
                "Date": dates.strftime("%Y-%m-%d"),
                "IsOpen": (dates.weekday < 5).astype(int),
                "PreviousTradingDate": [pd.NaT] * len(dates),
            }
        ), {"vendor": "longbridge-test", "primary_key": ["Date"]}

    return Dataflows(
        {
            Dataset.STOCK_OHLCV.value: adjusted,
            Dataset.STOCK_UNADJUSTED_DAILY.value: execution,
            Dataset.TRADING_CALENDAR.value: calendar,
        }
    )


def test_multi_symbol_us_history_is_atomic_and_identity_pinned(
    functional_repo: Path,
) -> None:
    context = RepositoryContext.discover(functional_repo)
    command = PrepareUSHistoryCommand(
        ("MU", "AAPL.US"),
        date(2024, 1, 2),
        date(2024, 1, 3),
        daily_start=date(2024, 1, 2),
        frequencies=("30m", "daily", "weekly"),
    )

    first = prepare_us_history(context, command, dataflows=_dataflows())
    second = prepare_us_history(context, command, dataflows=_dataflows())

    assert first.status == "PASS"
    assert first.result["symbols"] == ["MU.US", "AAPL.US"]
    assert first.result["generation_id"] == second.result["generation_id"]
    manifest_path = Path(first.artifacts["manifest"])
    assert manifest_path == Path(second.artifacts["manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["symbols"] == ["MU.US", "AAPL.US"]
    assert set(manifest["symbol_manifests"]) == {"MU.US", "AAPL.US"}
    assert len(manifest["calendar_identity"]["content_sha256"]) == 64
    generation = manifest_path.parent
    assert (generation / "calendar.csv").is_file()
    assert (generation / "MU.US" / "MU_30m_2024.csv").is_file()
    assert (generation / "MU.US" / "MU_execution_daily_2024.csv").is_file()
    assert (generation / "AAPL.US" / "AAPL_weekly_2024.csv").is_file()


def test_us_history_parser_accepts_repeated_symbols_and_split_starts() -> None:
    from czsc_trader.cli.main import build_parser

    args = build_parser().parse_args(
        [
            "data",
            "prepare-us-history",
            "--symbol",
            "MU.US",
            "--symbol",
            "AAPL.US",
            "--start",
            "2023-09-01",
            "--daily-start",
            "1997-01-02",
            "--end",
            "2026-09-18",
            "--frequency",
            "30m",
            "--frequency",
            "daily",
        ]
    )

    assert args.symbol == ["MU.US", "AAPL.US"]
    assert args.daily_start == date(1997, 1, 2)
    assert args.frequency == ["30m", "daily"]


def test_us_history_rejects_non_us_symbol_before_vendor_normalization(
    functional_repo: Path,
) -> None:
    context = RepositoryContext.discover(functional_repo)

    with pytest.raises(Exception, match="US equity symbols only"):
        prepare_us_history(
            context,
            PrepareUSHistoryCommand(
                ("00700.HK",),
                date(2024, 1, 2),
                date(2024, 1, 3),
            ),
            dataflows=_dataflows(),
        )
