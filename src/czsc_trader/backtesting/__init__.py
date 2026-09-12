"""Deterministic strategy replay contracts."""

from .models import StrategyIdentity, StrategySnapshot
from .execution_replay import replay_account
from .result import BacktestResult
from .signal_replay import SignalReplay, replay_signals
from .service import BacktestRequestV2, BacktestRunSummary, run_backtest_v2
from .datasets import DatasetName, ReplayData, load_replay_data
from .strategy_source import resolve_candidate_snapshot, resolve_registered_strategy
from .closing_dislocation_replay import build_closing_dislocation_signals

__all__ = [
    "StrategyIdentity",
    "StrategySnapshot",
    "DatasetName",
    "ReplayData",
    "load_replay_data",
    "BacktestResult",
    "SignalReplay",
    "replay_account",
    "replay_signals",
    "BacktestRequestV2",
    "BacktestRunSummary",
    "run_backtest_v2",
    "resolve_candidate_snapshot",
    "resolve_registered_strategy",
    "build_closing_dislocation_signals",
]
