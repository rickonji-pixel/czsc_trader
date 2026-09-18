"""Trading Execution Engine (TXE) public contracts."""

from .daily import DailyExecutionResult, execute_target_positions
from .fills import FillDecision, OrderSpec, resolve_fill
from .historical import HistoricalExecutor
from .result import ExecutionResult

EXECUTION_CONTRACT_VERSION = "TXE-v1"

__all__ = [
    "DailyExecutionResult",
    "EXECUTION_CONTRACT_VERSION",
    "FillDecision",
    "HistoricalExecutor",
    "ExecutionResult",
    "OrderSpec",
    "execute_target_positions",
    "resolve_fill",
]
