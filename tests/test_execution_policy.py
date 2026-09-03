from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_execution_policy_rounds_buy_limit_down_to_etf_tick() -> None:
    from czsc_trader.execution_policy import floor_to_tick

    assert floor_to_tick(1.68899) == pytest.approx(1.688)
    assert floor_to_tick(1.68900) == pytest.approx(1.689)


def test_execution_policy_retries_entry_without_changing_actual_position() -> None:
    from czsc_trader.execution_policy import simulate_limit_policy

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
    session_times = ("10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00")
    for date in dates:
        for position, value in enumerate(session_times):
            low = 10.4
            if date == dates[3] and position == 1:
                low = 10.1
            intraday_rows.append(
                {
                    "dt": pd.Timestamp(f"{date.date()} {value}"),
                    "open": 10.5,
                    "high": 10.7,
                    "low": low,
                    "close": 10.5,
                    "vol": 1_000_000.0,
                }
            )
    intraday = pd.DataFrame(intraday_rows)
    target = pd.Series([0.0, 1.0, 1.0, 0.0, 0.0], index=dates)
    limits = pd.Series([9.9, 10.0, 10.2, 10.0, 10.0], index=dates)

    result = simulate_limit_policy(daily, intraday, target, limits)

    assert result.daily_state.loc[dates[2], "actual_position"] == 0.0
    assert result.daily_state.loc[dates[3], "actual_position"] == 1.0
    assert result.daily_state.loc[dates[4], "actual_position"] == 0.0
    assert result.orders["side"].tolist() == ["Buy", "Sell"]
    assert result.orders.iloc[0]["execution_date"] == pd.Timestamp("2026-01-07 10:30")
    assert result.orders.iloc[0]["price"] == pytest.approx(10.2)
    assert result.cycles.iloc[0]["wait_sessions"] == 2
    assert bool(result.cycles.iloc[0]["filled"])


def test_execution_policy_uses_round_lots_and_requires_price_penetration() -> None:
    from czsc_trader.execution_policy import simulate_limit_policy

    dates = pd.bdate_range("2026-01-05", periods=3)
    daily = pd.DataFrame({
        "dt": dates,
        "open": [10.0, 10.2, 10.0],
        "high": [10.2, 10.3, 10.2],
        "low": [9.9, 10.0, 9.9],
        "close": [10.0, 10.1, 10.1],
    })
    intraday = pd.DataFrame({
        "dt": [pd.Timestamp("2026-01-06 10:00"), pd.Timestamp("2026-01-07 10:00")],
        "low": [10.0, 9.9],
        "vol": [1_000_000.0, 1_000_000.0],
    })
    target = pd.Series([1.0, 1.0, 1.0], index=dates)
    limits = pd.Series([10.0, 10.0, 10.0], index=dates)

    result = simulate_limit_policy(
        daily, intraday, target, limits, init_cash=100_055.0,
        lot_size=100, fill_on_equal_touch=False,
    )

    assert result.daily_state.loc[dates[1], "actual_position"] == 0.0
    assert result.orders.iloc[0]["execution_date"] == pd.Timestamp("2026-01-07")
    assert result.orders.iloc[0]["size"] % 100 == 0
    assert result.daily_state.iloc[-1]["cash"] >= 0


def test_execution_policy_applies_adverse_slippage_to_actual_fill_prices() -> None:
    from czsc_trader.execution_policy import simulate_limit_policy

    dates = pd.bdate_range("2026-01-05", periods=4)
    daily = pd.DataFrame({
        "dt": dates,
        "open": [10.0, 10.0, 10.0, 11.0],
        "high": [10.2, 10.2, 10.6, 11.2],
        "low": [9.8, 9.8, 9.8, 10.8],
        "close": [10.0, 10.0, 10.5, 11.0],
    })
    intraday = pd.DataFrame({
        "dt": [pd.Timestamp(f"{day.date()} 10:00") for day in dates],
        "low": [9.8, 9.8, 9.8, 10.8],
        "vol": [1_000_000.0] * 4,
    })
    target = pd.Series([0.0, 1.0, 0.0, 0.0], index=dates)
    limits = pd.Series([10.0] * 4, index=dates)

    result = simulate_limit_policy(
        daily, intraday, target, limits, slippage_bp=100,
    )

    assert result.orders["side"].tolist() == ["Buy", "Sell"]
    assert result.orders.iloc[0]["price"] == pytest.approx(10.1)
    assert result.orders.iloc[1]["price"] == pytest.approx(10.89)


def test_execution_policy_selector_enforces_service_level_then_price() -> None:
    from czsc_trader.execution_policy import select_execution_candidate

    candidates = pd.DataFrame(
        [
            {
                "candidate_id": "cheap",
                "t1_fill_rate": 0.89,
                "cap_p95": 0.001,
                "cap_mean": 0.0,
                "worst_calmar": 3.0,
                "worst_drawdown": -0.05,
                "family_rank": 0,
                "parameter": 0.0,
            },
            {
                "candidate_id": "fixed",
                "t1_fill_rate": 0.91,
                "cap_p95": 0.010,
                "cap_mean": 0.005,
                "worst_calmar": 1.0,
                "worst_drawdown": -0.10,
                "family_rank": 0,
                "parameter": 0.5,
            },
            {
                "candidate_id": "atr",
                "t1_fill_rate": 0.95,
                "cap_p95": 0.012,
                "cap_mean": 0.004,
                "worst_calmar": 2.0,
                "worst_drawdown": -0.08,
                "family_rank": 1,
                "parameter": 0.5,
            },
        ]
    )

    assert select_execution_candidate(candidates)["candidate_id"] == "fixed"


def test_execution_experiment_requires_frozen_policy_before_test_access(tmp_path: Path) -> None:
    runner_path = REPO_ROOT / "experiments" / "0902_EX02" / "run_experiment.py"
    spec = importlib.util.spec_from_file_location("execution_policy_experiment", runner_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    frozen = tmp_path / "frozen_execution_policy.json"

    with pytest.raises(ValueError, match="frozen execution policy"):
        module.assert_freeze_before_test(frozen, test_accessed=False)

    frozen.write_text('{"status":"FROZEN"}\n', encoding="utf-8")
    module.assert_freeze_before_test(frozen, test_accessed=False)
    with pytest.raises(ValueError, match="already accessed"):
        module.assert_freeze_before_test(frozen, test_accessed=True)


def test_daily_advice_maps_target_and_actual_position_to_manual_action() -> None:
    from czsc_trader.application.advice_service import build_advice
    from czsc_trader.execution_policies import resolve_execution_policy

    policy = resolve_execution_policy(
        REPO_ROOT / "configs" / "execution_policies",
        symbol="588080.SH",
        baseline_version="baseline_20260901",
        baseline_sha256="711254af3fe951cc0eb32c81121ef52233a0cf2577f46b14683b2f3e6b961993",
        required=True,
    )
    assert policy is not None
    common = {
        "signal_date": pd.Timestamp("2026-09-01"),
        "valid_session": pd.Timestamp("2026-09-02"),
        "signal_close": 1.704,
        "execution_close": 1.688,
        "quantity": 50_000,
        "policy": policy,
    }

    cash = build_advice(target_position=0, actual_position=0, **common)
    entry = build_advice(target_position=1, actual_position=0, **common)
    holding = build_advice(target_position=1, actual_position=1, **common)
    exit_advice = build_advice(target_position=0, actual_position=1, **common)

    assert (cash["state"], cash["action"]) == ("CASH", "WAIT")
    assert (entry["state"], entry["action"]) == ("PENDING_ENTRY", "BUY")
    assert entry["order"]["order_type"] == "限价委托"
    assert entry["order"]["maximum_buy_price"] == pytest.approx(1.688)
    assert entry["order"]["low_open_warning_price"] == pytest.approx(1.676)
    assert entry["signal_reference_price"] == pytest.approx(1.704)
    assert entry["execution_reference_price"] == pytest.approx(1.688)
    assert entry["order"]["valid_for"] == "NEXT_TRADING_SESSION"
    assert (holding["state"], holding["action"]) == ("HOLDING", "HOLD")
    assert (exit_advice["state"], exit_advice["action"]) == ("PENDING_EXIT", "SELL")
    assert exit_advice["order"]["primary_order_type"] == "限价委托"
    assert exit_advice["order"]["limit_price"] == pytest.approx(1.350)

    with pytest.raises(ValueError, match="100-share lots"):
        build_advice(target_position=1, actual_position=0, quantity=50_050, **{
            key: value for key, value in common.items() if key != "quantity"
        })


def test_advice_v1_maps_share_delta_to_broker_ready_limit_orders() -> None:
    from czsc_trader.application.advice_service import build_advice_v1
    from czsc_trader.execution_policies import resolve_execution_policy

    policy = resolve_execution_policy(
        REPO_ROOT / "configs" / "execution_policies",
        symbol="588080.SH",
        baseline_version="baseline_20260901",
        baseline_sha256="711254af3fe951cc0eb32c81121ef52233a0cf2577f46b14683b2f3e6b961993",
        required=True,
    )
    assert policy is not None
    common = {
        "symbol": "588080.SH",
        "signal_date": pd.Timestamp("2026-09-01"),
        "valid_session": pd.Timestamp("2026-09-02"),
        "signal_close": 1.7043736,
        "execution_close": 1.688,
        "position_size": 50_000,
        "policy": policy,
        "baseline_version": "baseline_20260901",
        "baseline_sha256": "711254af3fe951cc0eb32c81121ef52233a0cf2577f46b14683b2f3e6b961993",
    }

    entry = build_advice_v1(target_position=1, actual_quantity=20_000, **common)
    exit_advice = build_advice_v1(target_position=0, actual_quantity=20_000, **common)
    repeat = build_advice_v1(target_position=1, actual_quantity=20_000, **common)

    assert entry["contract_version"] == "advice.v1"
    assert entry["valid_session"] == "2026-09-02"
    assert entry["actual_quantity"] == 20_000
    assert entry["target_quantity"] == 50_000
    assert entry["delta_quantity"] == 30_000
    assert entry["order"] == {
        "side": "BUY",
        "quantity": 30_000,
        "order_type": "LIMIT",
        "limit_price": pytest.approx(1.688),
        "time_in_force": "DAY",
    }
    assert repeat["decision_id"] == entry["decision_id"]
    assert exit_advice["target_quantity"] == 0
    assert exit_advice["delta_quantity"] == -20_000
    assert exit_advice["order"] == {
        "side": "SELL",
        "quantity": 20_000,
        "order_type": "LIMIT",
        "limit_price": pytest.approx(1.350),
        "time_in_force": "DAY",
    }


def test_advice_v1_uses_published_exchange_session_instead_of_weekday_offset() -> None:
    from czsc_trader.application.advice_service import build_advice_v1
    from czsc_trader.execution_policies import resolve_execution_policy

    policy = resolve_execution_policy(
        REPO_ROOT / "configs" / "execution_policies",
        symbol="588080.SH",
        baseline_version="baseline_20260901",
        baseline_sha256="711254af3fe951cc0eb32c81121ef52233a0cf2577f46b14683b2f3e6b961993",
        required=True,
    )
    result = build_advice_v1(
        symbol="588080.SH",
        signal_date=pd.Timestamp("2026-09-30"),
        valid_session=pd.Timestamp("2026-10-09"),
        signal_close=1.7,
        execution_close=1.7,
        target_position=0,
        actual_quantity=0,
        position_size=50_000,
        policy=policy,
        baseline_version="baseline_20260901",
        baseline_sha256="711254af3fe951cc0eb32c81121ef52233a0cf2577f46b14683b2f3e6b961993",
    )
    assert result["valid_session"] == "2026-10-09"


def test_advice_v1_rejects_invalid_account_quantities() -> None:
    from czsc_trader.application.advice_service import validate_quantity_input

    with pytest.raises(ValueError, match="actual quantity"):
        validate_quantity_input(-100, 50_000)
    with pytest.raises(ValueError, match="100-share lots"):
        validate_quantity_input(50, 50_000)
    with pytest.raises(ValueError, match="position size"):
        validate_quantity_input(0, 0)


def test_advice_v2_uses_all_fee_covered_cash_in_round_lots() -> None:
    from czsc_trader.application.advice_service import build_advice_v2
    from czsc_trader.execution_policies import resolve_execution_policy

    policy = resolve_execution_policy(
        REPO_ROOT / "configs" / "execution_policies", symbol="588080.SH",
        baseline_version="baseline_20260901",
        baseline_sha256="711254af3fe951cc0eb32c81121ef52233a0cf2577f46b14683b2f3e6b961993",
        required=True,
    )
    result = build_advice_v2(
        symbol="588080.SH", signal_date=pd.Timestamp("2026-09-01"),
        valid_session=pd.Timestamp("2026-09-02"), signal_close=1.7043736,
        execution_close=1.688, target_position=1, actual_quantity=0,
        available_cash=1_000_000.0, policy=policy,
        baseline_version="baseline_20260901",
        baseline_sha256="711254af3fe951cc0eb32c81121ef52233a0cf2577f46b14683b2f3e6b961993",
    )
    expected = int(1_000_000 / (1.688 * 1.0005) // 100 * 100)
    assert result["contract_version"] == "advice.v2"
    assert result["order"]["quantity"] == expected
    assert result["target_quantity"] == expected
    assert result["estimated_order_cost"] <= 1_000_000
    assert 0 <= result["unallocated_cash"] < 1.688 * 1.0005 * 100


def test_advice_v3_keeps_one_target_for_the_entry_cycle() -> None:
    from czsc_trader.application.advice_service import build_advice_v3
    from czsc_trader.baselines import resolve_baseline

    baseline = resolve_baseline(
        REPO_ROOT / "configs" / "rule_baselines",
        "baseline_20260903",
        symbol="588080.SH",
    )
    first = build_advice_v3(
        baseline=baseline,
        signal_date=pd.Timestamp("2026-09-01"),
        valid_session=pd.Timestamp("2026-09-02"),
        signal_close=1.704,
        execution_close=1.688,
        target_position=1,
        actual_quantity=0,
        available_cash=1_000_000.0,
        cycle_target_quantity=None,
    )
    target = first["cycle_target_quantity"]
    retry = build_advice_v3(
        baseline=baseline,
        signal_date=pd.Timestamp("2026-09-02"),
        valid_session=pd.Timestamp("2026-09-03"),
        signal_close=1.67,
        execution_close=1.65,
        target_position=1,
        actual_quantity=target,
        available_cash=10_000.0,
        cycle_target_quantity=target,
    )

    assert first["contract_version"] == "advice.v3"
    assert "execution_policy" not in first
    assert target % 100 == 0
    assert retry["target_quantity"] == target
    assert retry["delta_quantity"] == 0
    assert retry["action"] == "HOLD"
    assert retry["order"] is None


def test_advice_v4_uses_release_identity_without_mutable_display_fields() -> None:
    from czsc_trader.application.advice_service import build_advice_v4
    from czsc_trader.baselines import resolve_baseline

    baseline = resolve_baseline(
        REPO_ROOT / "configs" / "rule_baselines",
        "baseline_20260903",
        symbol="588080.SH",
    )
    identity = {
        "strategy_id": "S001",
        "name": "综合基线策略",
        "version": "v1",
        "release_id": "S001-v1",
        "release_hash": "a" * 64,
        "qualification": "PAPER_READY",
    }
    common = {
        "baseline": baseline,
        "signal_date": pd.Timestamp("2026-09-01"),
        "valid_session": pd.Timestamp("2026-09-02"),
        "signal_close": 1.704,
        "execution_close": 1.688,
        "target_position": 1,
        "actual_quantity": 0,
        "available_cash": 100_000.0,
        "cycle_target_quantity": None,
    }

    first = build_advice_v4(strategy=identity, **common)
    renamed = build_advice_v4(
        strategy={**identity, "name": "新展示名称", "qualification": "LIVE_READY"},
        **common,
    )

    assert first["contract_version"] == "advice.v4"
    assert first["strategy"] == identity
    assert "baseline" not in first
    assert first["decision_id"] == renamed["decision_id"]
