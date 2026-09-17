from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from strategy_runtime import RuntimeContractError

from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.runtime_acceptance import validate_runtime_readiness
from czsc_trader.backtesting import load_replay_data, resolve_registered_strategy
from czsc_trader.backtesting.channel import BacktestChannel
from czsc_trader.backtesting.causal_feature_gate_replay import (
    build_causal_feature_gate_signals,
)
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.backtesting.intraday_overlay_replay import (
    build_moneyflow_breadth_signals,
    replay_intraday_overlay,
)
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.backtesting.signal_replay import replay_signals
from czsc_trader.backtesting.srt_bridge import (
    build_srt_signal_replay,
    replay_srt_account,
)
from strategy_manager import StrategyRegistry


ROOT = Path(__file__).resolve().parents[2]
START = pd.Timestamp("2026-06-25")
END = pd.Timestamp("2026-09-02")


@pytest.mark.parametrize(
    ("family", "version", "symbol"),
    (
        ("S001", "v1", "588080.SH"),
        ("S001", "v2", "588080.SH"),
        ("S002", "v1", "510500.SH"),
        ("S003", "v1", "510500.SH"),
        ("S007", "v1", "588080.SH"),
    ),
)
def test_srt_backtest_matches_complete_legacy_ledger(
    family: str, version: str, symbol: str
) -> None:
    context = RepositoryContext.discover(ROOT)
    readiness = validate_runtime_readiness(
        StrategyRegistry(context.strategy_root).get_version(family, version)
    )
    assert readiness["status"] == "PASS"
    assert readiness["release_id"] == f"{family}-{version}"
    snapshot = resolve_registered_strategy(context, family, version)
    replay_data = load_replay_data(
        context,
        "backtest",
        symbol,
        "etf",
        END.date(),
        include_five_minute=family == "S003",
    )
    if family == "S003":
        legacy_signals = build_moneyflow_breadth_signals(snapshot, replay_data, ROOT, START, END)
        legacy = replay_intraday_overlay(legacy_signals, replay_data, 100_000)
    elif family == "S007":
        legacy_signals = build_causal_feature_gate_signals(snapshot, replay_data, START, END, ROOT)
        legacy = replay_account(legacy_signals, replay_data, 100_000)
    else:
        legacy_signals = replay_signals(snapshot, replay_data, START.date(), END.date())
        legacy = replay_account(legacy_signals, replay_data, 100_000)

    strategy, srt_signals = build_srt_signal_replay(
        snapshot=snapshot,
        replay_data=replay_data,
        start=START,
        end=END,
        repository_root=ROOT,
    )
    actual = replay_srt_account(
        strategy=strategy,
        signals=srt_signals,
        replay_data=replay_data,
        initial_cash=100_000,
    )

    for name in ("decisions", "orders", "fills", "account_daily", "trades"):
        pd.testing.assert_frame_equal(
            getattr(actual, name).reset_index(drop=True),
            getattr(legacy, name).reset_index(drop=True),
            check_dtype=False,
        )
    assert calculate_metrics(actual, 100_000) == calculate_metrics(legacy, 100_000)


def test_tdr_backtest_channel_refuses_to_finalize_an_unsubmitted_decision() -> None:
    signals = SimpleNamespace(
        decisions=pd.DataFrame(
            [{
                "decision_id": "DEC-MISSING",
                "valid_session": pd.Timestamp("2026-09-18"),
                "target_position": 1.0,
            }]
        )
    )
    channel = BacktestChannel(
        signals=signals,
        replay_data=None,
        initial_cash=100_000,
        output_kind="TARGET_POSITION",
        order_types=("LIMIT",),
    )

    with pytest.raises(RuntimeContractError, match="decision sequence differs"):
        channel.finalize()
