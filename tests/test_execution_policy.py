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
        "close": 1.688,
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
    assert entry["order"]["valid_for"] == "NEXT_TRADING_SESSION"
    assert (holding["state"], holding["action"]) == ("HOLDING", "HOLD")
    assert (exit_advice["state"], exit_advice["action"]) == ("PENDING_EXIT", "SELL")
    assert exit_advice["order"]["primary_order_type"] == "限价委托"
    assert "券商显示的当日合法价格下限" in exit_advice["order"]["price_instruction"]

    with pytest.raises(ValueError, match="100-share lots"):
        build_advice(target_position=1, actual_position=0, quantity=50_050, **{
            key: value for key, value in common.items() if key != "quantity"
        })
