"""Deterministic strategy replay contracts."""

from .models import StrategyIdentity, StrategySnapshot
from .result import BacktestResult
from .signal_replay import SignalReplay
from .service import BacktestRequestV2, BacktestRunSummary, run_backtest_v2
from .datasets import DatasetName, ReplayData, load_replay_data
from .strategy_source import resolve_candidate_snapshot, resolve_registered_strategy

__all__ = [
    "StrategyIdentity",
    "StrategySnapshot",
    "DatasetName",
    "ReplayData",
    "load_replay_data",
    "BacktestResult",
    "SignalReplay",
    "BacktestRequestV2",
    "BacktestRunSummary",
    "run_backtest_v2",
    "resolve_candidate_snapshot",
    "resolve_registered_strategy",
]
