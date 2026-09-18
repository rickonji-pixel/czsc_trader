from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from strategy_runtime import (
    DeploymentSpec,
    ExecutionPolicy,
    RuntimeContractError,
    StrategyDecision,
    StrategyRunner,
)

from czsc_trader.backtesting.channel import BacktestChannel
from czsc_trader.backtesting.models import StrategyIdentity
from czsc_trader.backtesting.srt_bridge import _validate_historical_decisions


def test_srt_history_rejects_missing_required_score_instead_of_silent_hold() -> None:
    sessions = pd.DatetimeIndex(pd.to_datetime(["2026-09-16", "2026-09-17"]), name="dt")
    history = pd.DataFrame(
        {
            "base_score": [0.2, float("nan")],
            "confirmation_score": [0.1, 0.1],
            "target_position": [0.0, 0.0],
        },
        index=sessions,
    )

    with pytest.raises(RuntimeContractError, match="base_score"):
        _validate_historical_decisions(history, sessions)


def test_tdr_backtest_channel_refuses_to_finalize_an_unsubmitted_decision() -> None:
    daily = pd.DataFrame(
        [{"dt": pd.Timestamp("2026-09-18"), "open": 1.0, "close": 1.0}]
    )
    channel = BacktestChannel(
        identity=StrategyIdentity("REGISTERED", "S001-v1", "test"),
        replay_data=SimpleNamespace(
            execution_daily=daily,
            execution_intraday=pd.DataFrame(
                columns=["dt", "open", "high", "low", "close"]
            ),
            adjusted=SimpleNamespace(daily=daily),
        ),
        evaluation_start=pd.Timestamp("2026-09-18"),
        evaluation_end=pd.Timestamp("2026-09-18"),
        initial_cash=100_000,
        execution_policy=ExecutionPolicy(
            "FROZEN_RULE",
            {
                "capital": {"fee_rate": 0.001, "mode": "full_available_cash"},
                "entry": {"limit_parameter": 0.0, "order_type": "LIMIT"},
                "exit": {"limit_ratio": 0.1, "order_type": "MARKET"},
                "instrument": {
                    "lot_size": 100,
                    "maximum_order_quantity": 1_000_000,
                    "price_limit_ratio": 0.1,
                    "price_tick": 0.001,
                },
            },
        ),
        order_types=("LIMIT",),
    )

    with pytest.raises(RuntimeContractError, match="incomplete target-position requests"):
        channel.finalize()


def test_tdr_backtest_channel_executes_requests_against_its_confirmed_ledger() -> None:
    daily = pd.DataFrame(
        [
            {"dt": pd.Timestamp("2026-09-16"), "open": 1.0, "close": 1.0},
            {"dt": pd.Timestamp("2026-09-17"), "open": 1.0, "close": 1.1},
            {"dt": pd.Timestamp("2026-09-18"), "open": 1.1, "close": 1.1},
        ]
    )
    intraday = pd.DataFrame(
        [
            {
                "dt": pd.Timestamp("2026-09-17 10:00"),
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
            },
            {
                "dt": pd.Timestamp("2026-09-18 10:00"),
                "open": 1.1,
                "high": 1.1,
                "low": 1.1,
                "close": 1.1,
            },
        ]
    )
    replay_data = SimpleNamespace(
        execution_daily=daily,
        execution_intraday=intraday,
        adjusted=SimpleNamespace(daily=daily),
    )
    policy = ExecutionPolicy(
        "FROZEN_RULE",
        {
            "capital": {
                "allocation_fraction": 1.0,
                "fee_rate": 0.001,
                "mode": "full_available_cash",
                "target_scope": "entry_cycle",
            },
            "entry": {"limit_parameter": 0.0, "order_type": "LIMIT"},
            "exit": {"limit_ratio": 0.1, "order_type": "LIMIT"},
            "instrument": {
                "lot_size": 100,
                "maximum_order_quantity": 1_000_000,
                "price_limit_ratio": 0.1,
                "price_tick": 0.001,
            },
        },
    )
    channel = BacktestChannel(
        identity=StrategyIdentity("REGISTERED", "S999-v1", "test"),
        replay_data=replay_data,
        evaluation_start=pd.Timestamp("2026-09-17"),
        evaluation_end=pd.Timestamp("2026-09-18"),
        initial_cash=100_000,
        execution_policy=policy,
        order_types=("LIMIT",),
    )
    zone = ZoneInfo("Asia/Shanghai")
    release_hash = "a" * 64
    runtime_hash = "b" * 64
    input_hash = "c" * 64
    runner = StrategyRunner()
    for revision, (signal_date, valid_date, target) in enumerate(
        (
            ("2026-09-16", "2026-09-17", 1.0),
            ("2026-09-17", "2026-09-18", 0.0),
        )
    ):
        generated_at = datetime.fromisoformat(f"{signal_date}T20:30:00").replace(tzinfo=zone)
        account = channel.account_snapshot("account", generated_at)
        assert account.revision == revision
        deployment = DeploymentSpec(
            "deployment",
            "S999-v1",
            release_hash,
            "588080.SH",
            "account",
            channel.channel_id,
            channel.deployment_settings,
        )
        decision = StrategyDecision(
            f"DEC-{revision}",
            deployment.deployment_id,
            deployment.release_id,
            deployment.release_hash,
            runtime_hash,
            generated_at,
            datetime.fromisoformat(f"{valid_date}T09:30:00").replace(tzinfo=zone),
            target,
            account.revision,
            0,
            {"test": input_hash},
            {"signal_date": signal_date},
            {"target_position": target},
        )
        strategy = SimpleNamespace(
            definition=SimpleNamespace(
                release_id="S999-v1",
                release_hash=release_hash,
                runtime_sha256=runtime_hash,
                capabilities=SimpleNamespace(order_types=("LIMIT",), checkpoints=()),
                decision=SimpleNamespace(minimum_target=0.0, maximum_target=1.0),
                execution=policy,
            )
        )
        runner.submit_precomputed(
            strategy=strategy,
            deployment=deployment,
            account_snapshot=account,
            channel=channel,
            decision=decision,
        )

    result = channel.finalize()
    assert [receipt.status for receipt in channel.receipts] == ["SETTLED", "SETTLED"]
    assert result.orders["side"].tolist() == ["BUY", "SELL"]
    assert result.fills["side"].tolist() == ["BUY", "SELL"]
    assert result.account_daily["quantity"].tolist() == [99_900, 0]
    assert result.account_daily.iloc[-1]["cash"] > 109_000


def test_tdr_backtest_channel_executes_intraday_overlay_plan() -> None:
    daily = pd.DataFrame(
        [
            {"dt": pd.Timestamp("2026-09-16"), "open": 10.0, "close": 10.0},
            {"dt": pd.Timestamp("2026-09-17"), "open": 10.0, "close": 10.1},
        ]
    )
    five = pd.DataFrame(
        [
            {
                "dt": pd.Timestamp("2026-09-17 09:35"),
                "open": 10.0,
                "high": 10.1,
                "low": 9.9,
                "close": 10.0,
            },
            {
                "dt": pd.Timestamp("2026-09-17 11:30"),
                "open": 10.1,
                "high": 10.3,
                "low": 10.0,
                "close": 10.2,
            },
        ]
    )
    replay_data = SimpleNamespace(
        execution_daily=daily,
        execution_intraday=pd.DataFrame(
            columns=["dt", "open", "high", "low", "close"]
        ),
        execution_five_minute=five,
        adjusted=SimpleNamespace(daily=daily),
    )
    policy = ExecutionPolicy(
        "INTRADAY_OVERLAY",
        {
            "core_fraction": 0.5,
            "event_fraction": 0.5,
            "lot_size": 100,
            "one_way_cost": 0.00012,
        },
    )
    channel = BacktestChannel(
        identity=StrategyIdentity("REGISTERED", "S003-v1", "test"),
        replay_data=replay_data,
        evaluation_start=pd.Timestamp("2026-09-17"),
        evaluation_end=pd.Timestamp("2026-09-17"),
        initial_cash=100_000,
        execution_policy=policy,
        order_types=("LIMIT", "MARKET"),
        checkpoints=("OPEN", "11:30_CLOSE"),
    )
    zone = ZoneInfo("Asia/Shanghai")
    generated_at = datetime.fromisoformat("2026-09-16T20:30:00").replace(tzinfo=zone)
    account = channel.account_snapshot("account", generated_at)
    assert account.position_quantity == 4_900
    deployment = DeploymentSpec(
        "deployment",
        "S003-v1",
        "a" * 64,
        "510500.SH",
        "account",
        channel.channel_id,
        channel.deployment_settings,
    )
    decision = StrategyDecision(
        "DEC-OVERLAY",
        deployment.deployment_id,
        deployment.release_id,
        deployment.release_hash,
        "b" * 64,
        generated_at,
        datetime.fromisoformat("2026-09-17T09:30:00").replace(tzinfo=zone),
        1.0,
        account.revision,
        0,
        {"test": "c" * 64},
        {"signal_date": "2026-09-16", "action": "INTRADAY_LONG_OVERLAY"},
        {"event_active": True},
    )
    strategy = SimpleNamespace(
        definition=SimpleNamespace(
            release_id="S003-v1",
            release_hash="a" * 64,
            runtime_sha256="b" * 64,
            capabilities=SimpleNamespace(
                order_types=("LIMIT", "MARKET"),
                checkpoints=("OPEN", "11:30_CLOSE"),
            ),
            decision=SimpleNamespace(minimum_target=0.0, maximum_target=1.0),
            execution=policy,
        )
    )
    StrategyRunner().submit_precomputed(
        strategy=strategy,
        deployment=deployment,
        account_snapshot=account,
        channel=channel,
        decision=decision,
    )

    result = channel.finalize()
    assert result.orders["side"].tolist() == ["BUY", "SELL"]
    assert result.orders["order_type"].tolist() == ["LIMIT", "MARKET"]
    assert result.fills["price"].tolist() == [10.0, 10.2]
    assert result.account_daily["quantity"].tolist() == [4_900]
    assert result.trades["status"].tolist() == ["CLOSED"]
