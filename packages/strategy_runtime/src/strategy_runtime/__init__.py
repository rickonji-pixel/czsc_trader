"""Strategy Runtime (SRT) public contracts."""

from .contracts import (
    DataPreparationResult,
    ExecutionCapabilities,
    ExecutionOutcome,
    ExecutionPlan,
    ExecutionState,
    OrderSide,
    OrderType,
    PlanLeg,
    PlannedOrder,
    PortfolioSnapshot,
    PriceReference,
    StrategyIdentity,
    TradableWindow,
    TradingPoint,
    WindowExecutor,
)
from .errors import (
    RuntimeCompatibilityError,
    RuntimeContractError,
    RuntimeExecutionError,
    StrategyRuntimeError,
)
from .models import (
    ExecutionPolicy,
    RuntimeDefinition,
    StrategyCandidate,
    StrategyRelease,
    canonical_sha256,
)
from .runtime import StrategyInit, StrategyRuntime
from .strategy import StrategyInstance

__version__ = "0.1.0"

__all__ = [
    "DataPreparationResult",
    "ExecutionCapabilities",
    "ExecutionOutcome",
    "ExecutionPlan",
    "ExecutionPolicy",
    "ExecutionState",
    "OrderSide",
    "OrderType",
    "PlanLeg",
    "PlannedOrder",
    "PortfolioSnapshot",
    "PriceReference",
    "RuntimeCompatibilityError",
    "RuntimeContractError",
    "RuntimeDefinition",
    "RuntimeExecutionError",
    "StrategyCandidate",
    "StrategyIdentity",
    "StrategyInit",
    "StrategyInstance",
    "StrategyRelease",
    "StrategyRuntime",
    "StrategyRuntimeError",
    "TradableWindow",
    "TradingPoint",
    "WindowExecutor",
    "canonical_sha256",
]
