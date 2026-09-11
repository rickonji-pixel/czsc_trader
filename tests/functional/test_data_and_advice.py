from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from czsc_trader import market_data_prep
from czsc_trader.application.advice_service import build_advice_v4
from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.data_service import _assert_append_only
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.backtesting.strategy_source import resolve_registered_strategy
from czsc_trader.baselines import resolve_baseline
from czsc_trader.data import load_execution_prices
from czsc_trader.execution_policy import floor_to_tick, simulate_limit_policy
from czsc_trader.execution_intent import decide_order_intent
from dataflows import tushare_etf

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


def test_shared_order_intent_uses_unadjusted_close_and_full_cash(
    functional_repo: Path,
) -> None:
    context = RepositoryContext.discover(functional_repo)
    snapshot = resolve_registered_strategy(context, "S001", "v1")
    intent = decide_order_intent(
        target_position=1,
        actual_quantity=0,
        cycle_target_quantity=None,
        available_cash=1_000_000,
        execution_close=1.430,
        execution_spec=snapshot.resolved_rule.execution,
    )

    assert intent.limit_price == 1.430
    assert intent.target_quantity == 698_900
    assert sum(order.quantity for order in intent.orders) == 698_900

    retry = decide_order_intent(
        target_position=1,
        actual_quantity=0,
        cycle_target_quantity=72_800,
        available_cash=100_000,
        execution_close=1.430,
        execution_spec=snapshot.resolved_rule.execution,
    )
    assert retry.target_quantity == 69_800
    assert retry.cycle_target_quantity == 72_800
    assert sum(order.quantity for order in retry.orders) == 69_800

    guarded_snapshot = resolve_registered_strategy(context, "S001", "v2")
    exit_intent = decide_order_intent(
        target_position=0,
        actual_quantity=1000,
        cycle_target_quantity=1000,
        available_cash=0,
        execution_close=1.609,
        execution_spec=guarded_snapshot.resolved_rule.execution,
    )
    assert exit_intent.limit_price == 1.609
    assert exit_intent.orders[0].order_type == "MARKET"


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


def test_etf_long_history_fetch_segments_adjustment_factors(monkeypatch) -> None:
    class FakePro:
        def __init__(self) -> None:
            self.factor_requests: list[tuple[str, str]] = []

        def fund_daily(self, **_kwargs):
            return pd.DataFrame([
                {"trade_date": "20130315", "open": 1, "high": 1, "low": 1,
                 "close": 1, "vol": 1, "amount": 1},
                {"trade_date": "20260908", "open": 2, "high": 2, "low": 2,
                 "close": 2, "vol": 1, "amount": 1},
            ])

        def fund_adj(self, *, start_date, end_date, **_kwargs):
            self.factor_requests.append((start_date, end_date))
            factors = pd.DataFrame([
                {"trade_date": "20130315", "adj_factor": 1.0},
                {"trade_date": "20200102", "adj_factor": 1.5},
                {"trade_date": "20260908", "adj_factor": 2.0},
            ])
            return factors.loc[
                factors["trade_date"].between(start_date, end_date)
            ].reset_index(drop=True)

    pro = FakePro()
    monkeypatch.setattr(tushare_etf, "get_tushare_pro", lambda _env=None: pro)

    bars, metadata = tushare_etf.fetch_etf_ohlcv(
        "510500.SH", "2013-03-15", "2026-09-08", "daily"
    )

    assert pro.factor_requests == [
        ("20130315", "20171231"),
        ("20180101", "20221231"),
        ("20230101", "20260908"),
    ]
    assert bars["Close"].tolist() == [1.0, 4.0]
    assert metadata["adjustment_factor_source"] == "fund_adj"


def test_ft_t02_advice_covers_entry_retry_hold_exit_and_fill_rules(
    functional_repo: Path,
) -> None:
    baseline = resolve_baseline(
        functional_repo / "strategies" / "dependencies" / "legacy_rule_baselines",
        "baseline_20260903",
        symbol="588080.SH",
    )
    strategy = {
        "strategy_id": "S001",
        "name": "综合基线策略",
        "version": "v1",
        "release_id": "S001-v1",
        "release_hash": "a" * 64,
        "qualification": "PAPER_READY",
    }
    common = {
        "strategy": strategy,
        "baseline": baseline,
        "signal_close": 1.704,
        "execution_close": 1.688,
        "available_cash": 100_000.0,
    }
    entry = build_advice_v4(
        signal_date=pd.Timestamp("2026-09-01"),
        valid_session=pd.Timestamp("2026-09-02"),
        target_position=1,
        actual_quantity=0,
        cycle_target_quantity=None,
        **common,
    )
    target = entry["target_quantity"]
    retry = build_advice_v4(
        signal_date=pd.Timestamp("2026-09-02"),
        valid_session=pd.Timestamp("2026-09-03"),
        target_position=1,
        actual_quantity=0,
        cycle_target_quantity=target,
        **{**common, "execution_close": 1.750},
    )
    holding = build_advice_v4(
        signal_date=pd.Timestamp("2026-09-02"),
        valid_session=pd.Timestamp("2026-09-03"),
        target_position=1,
        actual_quantity=target,
        cycle_target_quantity=target,
        **common,
    )
    exit_advice = build_advice_v4(
        signal_date=pd.Timestamp("2026-09-03"),
        valid_session=pd.Timestamp("2026-09-04"),
        target_position=0,
        actual_quantity=target,
        cycle_target_quantity=target,
        **common,
    )

    assert all(
        row["contract_version"] == "advice.v4"
        for row in (entry, retry, holding, exit_advice)
    )
    assert entry["valid_session"] == "2026-09-02"
    assert entry["order"]["side"] == "BUY"
    assert entry["order"]["quantity"] % 100 == 0
    assert entry["order"]["limit_price"] == pytest.approx(
        floor_to_tick(1.688 * (1 + baseline.execution.entry_limit_parameter))
    )
    assert entry["order"]["limit_price"] * 1000 % 1 == pytest.approx(0)
    assert retry["cycle_target_quantity"] == target
    assert retry["target_quantity"] == 57_100
    assert retry["order"]["quantity"] == 57_100
    assert holding["delta_quantity"] == 0
    assert holding["order"] is None
    assert exit_advice["order"]["side"] == "SELL"
    assert exit_advice["order"]["quantity"] == target
    assert exit_advice["order"]["order_type"] == "MARKET"
    assert entry["decision_id"] == build_advice_v4(
        signal_date=pd.Timestamp("2026-09-01"),
        valid_session=pd.Timestamp("2026-09-02"),
        target_position=1,
        actual_quantity=0,
        cycle_target_quantity=None,
        **{**common, "strategy": {**strategy, "name": "展示名称已修改"}},
    )["decision_id"]
    with pytest.raises(ValueError, match="actual quantity"):
        build_advice_v4(
            signal_date=pd.Timestamp("2026-09-01"),
            valid_session=pd.Timestamp("2026-09-02"),
            target_position=1,
            actual_quantity=50,
            cycle_target_quantity=None,
            **common,
        )

    dates = pd.bdate_range("2026-01-02", periods=5)
    daily = pd.DataFrame(
        {
            "dt": dates,
            "open": [10.0, 10.0, 11.0, 10.8, 10.4],
            "high": [10.2, 10.2, 11.2, 11.0, 10.6],
            "low": [9.8, 9.8, 10.5, 10.1, 10.2],
            "close": [10.0, 10.0, 10.8, 10.5, 10.3],
        }
    )
    intraday_rows = []
    for session in dates:
        for position, value in enumerate(
            ("10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00")
        ):
            intraday_rows.append(
                {
                    "dt": pd.Timestamp(f"{session.date()} {value}"),
                    "open": 10.5,
                    "high": 10.7,
                    "low": 10.1 if session == dates[3] and position == 1 else 10.4,
                    "close": 10.5,
                    "vol": 1_000_000.0,
                }
            )
    result = simulate_limit_policy(
        daily,
        pd.DataFrame(intraday_rows),
        pd.Series([0.0, 1.0, 1.0, 0.0, 0.0], index=dates),
        pd.Series([9.9, 10.0, 10.2, 10.0, 10.0], index=dates),
        init_cash=100_055.0,
        lot_size=100,
        slippage_bp=10,
    )
    assert result.daily_state.loc[dates[2], "actual_position"] == 0.0
    assert result.daily_state.loc[dates[3], "actual_position"] == 1.0
    assert result.daily_state.loc[dates[4], "actual_position"] == 0.0
    assert result.orders["side"].tolist() == ["Buy", "Sell"]
    assert result.orders.iloc[0]["size"] % 100 == 0
    assert result.orders.iloc[0]["price"] > result.orders.iloc[0]["entry_limit"]
    assert result.orders.iloc[1]["price"] < daily.iloc[-1]["open"]
