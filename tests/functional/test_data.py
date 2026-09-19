from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from czsc_trader import market_data_prep
from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.data_service import _assert_append_only
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.data import load_execution_prices
from functional_support import invoke_main, vendor_frame

def test_backtest_update_only_allows_current_week_roll_forward(
    tmp_path: Path,
) -> None:
    current = tmp_path / "588080_weekly_2026.csv"
    proposed = tmp_path / "proposed.csv"
    pd.DataFrame(
        [
            {"date": "2026-08-28", "close": "1.70"},
            {"date": "2026-09-02", "close": "1.67"},
        ]
    ).to_csv(current, index=False)
    pd.DataFrame(
        [
            {"date": "2026-08-28", "close": "1.70"},
            {"date": "2026-09-04", "close": "1.75"},
        ]
    ).to_csv(proposed, index=False)

    with pytest.raises(ValueError, match="mutate published rows"):
        _assert_append_only(current, proposed)
    _assert_append_only(
        current,
        proposed,
        mutable_terminal_period=pd.Period("2026-09-02", freq="W-SUN"),
    )

    historical = tmp_path / "588080_weekly_2025.csv"
    pd.DataFrame([{"date": "2025-12-31", "close": "1.50"}]).to_csv(
        historical, index=False
    )
    _assert_append_only(
        historical,
        historical,
        mutable_terminal_period=pd.Period("2026-09-02", freq="W-SUN"),
    )

    changed_history = pd.read_csv(proposed, dtype=str)
    changed_history.loc[0, "close"] = "1.71"
    changed_history.to_csv(proposed, index=False)
    with pytest.raises(ValueError, match="mutate published rows"):
        _assert_append_only(
            current,
            proposed,
            mutable_terminal_period=pd.Period("2026-09-02", freq="W-SUN"),
        )


def test_replay_dataset_is_explicit_cutoff_aligned_and_deterministic(
    functional_repo: Path,
) -> None:
    context = RepositoryContext.discover(functional_repo)
    first = load_replay_data(
        context,
        "backtest",
        "588080.SH",
        "etf",
        date(2026, 9, 2),
    )
    repeated = load_replay_data(
        context,
        "backtest",
        "588080.SH",
        "etf",
        date(2026, 9, 2),
    )

    assert first.dataset == "backtest"
    assert first.adjusted.daily["dt"].max().date() == date(2026, 9, 2)
    assert first.execution_daily["dt"].max().date() == date(2026, 9, 2)
    assert set(first.execution_intraday["dt"].dt.normalize()) == set(
        first.execution_daily["dt"]
    )
    assert first.fingerprint == repeated.fingerprint
    assert len(first.fingerprint) == 64

    midweek = load_replay_data(
        context,
        "backtest",
        "588080.SH",
        "etf",
        date(2026, 9, 1),
    )
    assert midweek.adjusted.daily["dt"].max().date() == date(2026, 9, 1)
    assert midweek.adjusted.weekly["dt"].max().date() < date(2026, 9, 1)



def test_ft_t01_data_prepare_validate_and_tamper_detection(
    functional_repo: Path, capsys, monkeypatch
) -> None:
    day = "2026-09-01"
    intraday = vendor_frame(
        [
            (f"{day} {value}:00", 1.704)
            for value in (
                "10:00",
                "10:30",
                "11:00",
                "11:30",
                "13:30",
                "14:00",
                "14:30",
                "15:00",
            )
        ]
    )
    intraday["Volume"] = 125.0
    intraday["Amount"] = 213.0
    adjusted = {
        "30m": intraday,
        "daily": vendor_frame([(day, 1.704)]),
        "weekly": vendor_frame([(day, 1.704)]),
    }
    original = market_data_prep.prepare_market_data

    def offline_prepare(symbol, asset, start, end, output_dir, *, env_file=None):
        def fetcher(_symbol, _asset, _start, _end, period):
            return adjusted[period], {
                "vendor": "functional-test",
                "vendor_symbol": symbol,
                "asset_type": asset,
                "period": period,
                "adjustment": "hfq",
                "adjustment_factor_source": "fixed",
                "adjustment_factor_sha256": "factor-hash",
            }

        def execution_fetcher(_symbol, _asset, _start, _end):
            return vendor_frame([(day, 1.688)]), {
                "vendor": "functional-test",
                "vendor_symbol": symbol,
                "asset_type": asset,
                "period": "daily",
                "adjustment": "none",
            }

        return original(
            symbol,
            asset,
            start,
            end,
            output_dir,
            fetcher=fetcher,
            execution_fetcher=execution_fetcher,
            calendar_fetcher=lambda _after: (
                date(2026, 9, 2),
                {"vendor": "functional-test", "exchange": "SSE"},
            ),
            session_calendar_fetcher=lambda _start, _end: (
                pd.DataFrame({"Date": [pd.Timestamp(day)], "IsOpen": [1]}),
                {"vendor": "functional-test", "exchange": "SSE"},
            ),
            name_fetcher=lambda _symbol, _asset: "科创50ETF",
        )

    monkeypatch.setattr(market_data_prep, "prepare_market_data", offline_prepare)
    root = ["--repo-root", str(functional_repo)]
    prepared = invoke_main(
        [
            "data",
            "prepare",
            "--symbol",
            "588080.SH",
            "--asset",
            "etf",
            "--start",
            day,
            "--end",
            day,
            *root,
        ],
        capsys,
    )
    validated = invoke_main(
        ["data", "validate", "--symbol", "588080.SH", *root], capsys
    )

    execution_manifest = functional_repo / "data" / "raw" / "588080_execution_manifest.json"
    assert Path(prepared["result"]["execution_price_manifest"]) == execution_manifest
    assert prepared["result"]["data_cutoff"] == day
    execution_metadata = pd.read_json(execution_manifest, typ="series")
    assert execution_metadata["next_trading_session"] == "2026-09-02"
    assert validated["result"]["frequencies"] == ["30m", "daily", "weekly"]
    with pytest.raises(ValueError, match="behind requested end"):
        offline_prepare(
            "588080.SH", "etf", date(2026, 9, 1), date(2026, 9, 2),
            functional_repo / "state" / "stale-data",
        )
    execution_csv = functional_repo / "data" / "raw" / "588080_execution_daily_2026.csv"
    execution_csv.write_bytes(execution_csv.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="SHA-256 differs"):
        load_execution_prices(functional_repo / "data" / "raw", "588080.SH", "etf")
