from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from czsc_trader import market_data_prep
from czsc_trader.application.advice_service import (
    build_advice_v4,
    build_intraday_overlay_advice_v5,
)
from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.data_service import _assert_append_only
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.backtesting.strategy_source import (
    resolve_candidate_snapshot,
    resolve_registered_strategy,
)
from czsc_trader.baselines import (
    CausalFeatureGateSpec,
    ConstituentMoneyflowIntradaySpec,
    resolve_baseline,
    resolve_strategy_payload,
)
from czsc_trader.causal_feature_gate_runtime import (
    latest_signal as latest_causal_signal,
    publish_support_data as publish_causal_support,
    seed_support_panel as seed_causal_support,
)
from czsc_trader.constituent_moneyflow_runtime import (
    latest_breadth_signal,
    publish_support_data,
    seed_support_panel,
)
from czsc_trader.data import load_execution_prices
from czsc_trader.execution_policy import floor_to_tick, simulate_limit_policy
from czsc_trader.execution_intent import decide_order_intent
from czsc_trader.identity import raw_file_sha256
from dataflows import tushare_etf
from paper_trading_engine.contracts import AdviceDecision

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


def test_shared_order_intent_uses_unadjusted_close_and_full_cash(
    functional_repo: Path,
) -> None:
    context = RepositoryContext.discover(functional_repo)
    snapshot = resolve_registered_strategy(context, "S001", "v1")
    candidate = resolve_candidate_snapshot(
        context,
        "S001-ORDER-INTENT",
        snapshot.strategy_payload,
        "e" * 64,
        "functional://order-intent",
    )
    assert candidate.resolved_rule is not None
    intent = decide_order_intent(
        target_position=1,
        actual_quantity=0,
        cycle_target_quantity=None,
        available_cash=1_000_000,
        execution_close=1.430,
        execution_spec=candidate.resolved_rule.execution,
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
        execution_spec=candidate.resolved_rule.execution,
    )
    assert retry.target_quantity == 69_800
    assert retry.cycle_target_quantity == 72_800
    assert sum(order.quantity for order in retry.orders) == 69_800

    guarded_snapshot = resolve_registered_strategy(context, "S001", "v2")
    guarded_candidate = resolve_candidate_snapshot(
        context,
        "S001-GUARDED-ORDER-INTENT",
        guarded_snapshot.strategy_payload,
        "f" * 64,
        "functional://guarded-order-intent",
    )
    assert guarded_candidate.resolved_rule is not None
    exit_intent = decide_order_intent(
        target_position=0,
        actual_quantity=1000,
        cycle_target_quantity=1000,
        available_cash=0,
        execution_close=1.609,
        execution_spec=guarded_candidate.resolved_rule.execution,
    )
    assert exit_intent.limit_price == 1.609
    assert exit_intent.orders[0].order_type == "MARKET"

    payload = deepcopy(guarded_snapshot.strategy_payload)
    payload["rule"]["execution"]["capital"] = {
        "mode": "available_cash_fraction",
        "allocation_fraction": 0.6,
        "fee_rate": 0.0005,
        "target_scope": "entry_cycle",
    }
    fractional = resolve_strategy_payload(
        context.strategy_dependency_root,
        payload,
        release_id="S001-v3",
        release_hash="d" * 64,
        symbol="588080.SH",
        repository_root=functional_repo,
    )
    fractional_intent = decide_order_intent(
        target_position=1,
        actual_quantity=0,
        cycle_target_quantity=None,
        available_cash=100_000,
        execution_close=1.430,
        execution_spec=fractional.execution,
    )
    assert fractional.execution is not None
    assert fractional.execution.capital.allocation_fraction == 0.6
    assert fractional_intent.target_quantity == 41_900
    payload["rule"]["execution"]["capital"]["allocation_fraction"] = 0.0
    with pytest.raises(ValueError, match="allocation fraction"):
        resolve_strategy_payload(
            context.strategy_dependency_root,
            payload,
            release_id="S001-v3",
            release_hash="d" * 64,
            symbol="588080.SH",
            repository_root=functional_repo,
        )


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
    parsed_entry = AdviceDecision.from_cli_payload(
        {"status": "PASS", "result": {**entry, "data_cutoff": "2026-09-01"}}
    )
    assert parsed_entry.capital_mode == "full_available_cash"
    assert parsed_entry.allocation_fraction == 1.0
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


def test_s003_runtime_signal_and_planned_advice_contract(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    payload = json.loads(
        (repo / "experiments/S003/20260911_S003_EX56/candidate_payload.json").read_text(
            encoding="utf-8"
        )
    )
    baseline = resolve_strategy_payload(
        repo / "strategies/dependencies",
        payload,
        release_id="S003-v1",
        release_hash="c" * 64,
        symbol="510500.SH",
        repository_root=repo,
    )
    spec = baseline.constituent_moneyflow_intraday
    assert spec is not None
    seed_support_panel(repo, tmp_path, "S003-v1", spec)
    triggered, breadth, threshold, coverage = latest_breadth_signal(
        tmp_path, "S003-v1", spec, pd.Timestamp("2026-09-08")
    )
    assert isinstance(triggered, bool)
    assert 0 <= breadth <= 1
    assert 0 <= threshold <= 1
    assert coverage >= 0.95

    strategy = {
        "strategy_id": "S003",
        "name": "成分资金流宽度早盘延续",
        "version": "v1",
        "release_id": "S003-v1",
        "release_hash": "c" * 64,
        "qualification": "PAPER_READY",
    }
    setup = build_intraday_overlay_advice_v5(
        strategy=strategy,
        baseline=baseline,
        signal_date=pd.Timestamp("2026-09-08"),
        valid_session=pd.Timestamp("2026-09-09"),
        signal_close=6.20,
        execution_close=7.61,
        actual_quantity=0,
        available_cash=100_000,
        cycle_target_quantity=None,
        event_triggered=triggered,
    )
    parsed_setup = AdviceDecision.from_cli_payload({
        "status": "PASS",
        "result": {**setup, "data_cutoff": "2026-09-08"},
    })
    assert parsed_setup.plan_mode == "CORE_SETUP"
    assert parsed_setup.signal_reference_price == pytest.approx(6.20)
    assert parsed_setup.execution_reference_price == pytest.approx(7.61)
    assert parsed_setup.plan_legs[0].order.limit_price == pytest.approx(8.371)

    rotation = build_intraday_overlay_advice_v5(
        strategy=strategy,
        baseline=baseline,
        signal_date=pd.Timestamp("2026-09-08"),
        valid_session=pd.Timestamp("2026-09-09"),
        signal_close=6.20,
        execution_close=7.61,
        actual_quantity=parsed_setup.cycle_target_quantity,
        available_cash=50_000,
        cycle_target_quantity=parsed_setup.cycle_target_quantity,
        event_triggered=True,
    )
    parsed_rotation = AdviceDecision.from_cli_payload({
        "status": "PASS",
        "result": {**rotation, "data_cutoff": "2026-09-08"},
    })
    assert parsed_rotation.action == "ROTATE"
    assert [leg.order.side for leg in parsed_rotation.plan_legs] == ["BUY", "SELL"]
    assert parsed_rotation.plan_legs[1].dependency_required_status == "FILLED_ALL"
    with pytest.raises(ValueError, match="execution close"):
        build_intraday_overlay_advice_v5(
            strategy=strategy,
            baseline=baseline,
            signal_date=pd.Timestamp("2026-09-08"),
            valid_session=pd.Timestamp("2026-09-09"),
            signal_close=6.20,
            execution_close=0,
            actual_quantity=0,
            available_cash=100_000,
            cycle_target_quantity=None,
            event_triggered=False,
        )


def test_s003_runtime_support_appends_one_published_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = pd.bdate_range("2026-01-02", periods=61)
    source = tmp_path / "source.csv.gz"
    rows = [
        {
            "dt": session.date().isoformat(),
            "con_code": code,
            "snapshot_date": sessions[0].date().isoformat(),
            "weight": 50.0,
            "net_mf_amount": amount,
            "observed_moneyflow": True,
        }
        for session in sessions[:60]
        for code, amount in (("000001.SZ", 1.0), ("600000.SH", -1.0))
    ]
    pd.DataFrame(rows).to_csv(
        source,
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
    )
    spec = ConstituentMoneyflowIntradaySpec(
        symbol="510500.SH",
        source_path=source.name,
        source_sha256=raw_file_sha256(source),
        minimum_observed_weight_ratio=0.95,
        threshold_lookback_sessions=5,
        threshold_quantile=0.8,
        core_fraction=0.5,
        event_fraction=0.5,
        entry_checkpoint="OPEN",
        exit_checkpoint="11:30_CLOSE",
        one_way_cost=0.00012,
        lot_size=100,
        maximum_events_per_day=1,
        t_plus_one_inventory_rotation=True,
    )

    class FakePro:
        def index_weight(self, **kwargs):
            return pd.DataFrame([
                {
                    "index_code": "000905.SH", "con_code": "000001.SZ",
                    "trade_date": sessions[0].strftime("%Y%m%d"), "weight": 50.0,
                },
                {
                    "index_code": "000905.SH", "con_code": "600000.SH",
                    "trade_date": sessions[0].strftime("%Y%m%d"), "weight": 50.0,
                },
            ])

        def moneyflow(self, **kwargs):
            return pd.DataFrame([
                {
                    "ts_code": "000001.SZ",
                    "trade_date": sessions[-1].strftime("%Y%m%d"),
                    "net_mf_amount": 2.0,
                },
                {
                    "ts_code": "600000.SH",
                    "trade_date": sessions[-1].strftime("%Y%m%d"),
                    "net_mf_amount": 3.0,
                },
            ])

    monkeypatch.setattr(
        "czsc_trader.data.load_market_data",
        lambda *args, **kwargs: SimpleNamespace(
            daily=pd.DataFrame({"dt": sessions})
        ),
    )
    result = publish_support_data(
        tmp_path,
        tmp_path / "runtime",
        "S003-v1",
        spec,
        sessions[-1].date().isoformat(),
        pro=FakePro(),
    )
    assert result["appended_sessions"] == 1
    assert result["coverage"] == {sessions[-1].date().isoformat(): 1.0}
    triggered, breadth, _, coverage = latest_breadth_signal(
        tmp_path / "runtime", "S003-v1", spec, sessions[-1]
    )
    assert triggered
    assert breadth == pytest.approx(1.0)
    assert coverage == pytest.approx(1.0)


def test_s007_runtime_seed_reproduces_frozen_cutoff_signal(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    payload = json.loads(
        (repo / "experiments/S007/20260915_S007_EX31/candidate_payload.json").read_text(
            encoding="utf-8"
        )
    )
    resolved = resolve_strategy_payload(
        repo / "strategies/dependencies",
        payload,
        release_id="S007-v1",
        release_hash="e" * 64,
        symbol="588080.SH",
        repository_root=repo,
    )
    spec = resolved.causal_feature_gate
    assert spec is not None
    seed_causal_support(repo, tmp_path, "S007-v1", spec)

    target, base, confirmation, features = latest_causal_signal(
        tmp_path, "S007-v1", spec, pd.Timestamp("2026-09-02")
    )

    assert target == 0
    assert base == pytest.approx(-0.13541246038190136)
    assert confirmation == pytest.approx(-0.21754281552423682)
    assert set(features) == set(dict(spec.orientations))


def test_s007_runtime_support_appends_causal_features(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = pd.bdate_range("2026-06-01", periods=41)
    feature_names = {
        "micro_share_change_5d_lag1",
        "tsfresh__log_volume_change__mean__lb20",
        "price_close_vwap_deviation",
        "risk_global_spx_return",
        "risk_chinext_turnover_z20",
        "risk_shibor_on_change_5d",
        "price_intraday_range",
    }
    source = tmp_path / "source.csv.gz"
    seeded = pd.DataFrame({"date": sessions[:40]})
    for position, name in enumerate(sorted(feature_names), start=1):
        seeded[name] = position / 100 + pd.Series(range(40)) / 10_000
    seeded.to_csv(
        source,
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
    )
    spec = CausalFeatureGateSpec(
        symbol="588080.SH",
        source_path=source.name,
        source_sha256=raw_file_sha256(source),
        orientations=tuple((name, 1) for name in sorted(feature_names)),
        base_weights=tuple((name, 0.2) for name in sorted(feature_names)[:5]),
        confirmation_weights=tuple((name, 0.5) for name in sorted(feature_names)[5:]),
        normalization_lookback_sessions=20,
        normalization_minimum_observations=10,
        entry_threshold=0.1,
        exit_threshold=0.0,
        confirmation_threshold=0.0,
    )
    market = pd.DataFrame(
        {
            "dt": sessions,
            "high": 2.1 + pd.Series(range(41)) / 100,
            "low": 1.9 + pd.Series(range(41)) / 100,
            "close": 2.0 + pd.Series(range(41)) / 100,
            "vol": 1_000_000 + pd.Series(range(41)) * 1_000,
            "amount": (2.0 + pd.Series(range(41)) / 100) * 1_000_000,
        }
    )

    class FakePro:
        def shibor(self, **_kwargs):
            return pd.DataFrame({
                "date": sessions.strftime("%Y%m%d"),
                "on": 1.0 + pd.Series(range(41)) / 100,
            })

        def index_dailybasic(self, **_kwargs):
            return pd.DataFrame({
                "trade_date": sessions.strftime("%Y%m%d"),
                "turnover_rate_f": 2.0 + pd.Series(range(41)) / 10,
            })

        def etf_share_size(self, **_kwargs):
            return pd.DataFrame({
                "trade_date": sessions.strftime("%Y%m%d"),
                "total_share": 100.0 + pd.Series(range(41)),
            })

        def index_global(self, **_kwargs):
            return pd.DataFrame({
                "trade_date": (sessions - pd.Timedelta(days=1)).strftime("%Y%m%d"),
                "pct_chg": 0.1 + pd.Series(range(41)) / 100,
            })

    monkeypatch.setattr(
        "czsc_trader.causal_feature_gate_runtime.load_market_data",
        lambda *_args, **_kwargs: SimpleNamespace(daily=market),
    )
    result = publish_causal_support(
        tmp_path,
        tmp_path / "runtime",
        "S007-v1",
        spec,
        sessions[-1].date().isoformat(),
        pro=FakePro(),
    )

    assert result["appended_sessions"] == 1
    assert result["support_last_session"] == sessions[-1].date().isoformat()
    target, base, confirmation, features = latest_causal_signal(
        tmp_path / "runtime", "S007-v1", spec, sessions[-1]
    )
    assert target in (0, 1)
    assert pd.notna(base) and pd.notna(confirmation)
    assert set(features) == feature_names
