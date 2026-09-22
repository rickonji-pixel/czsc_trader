"""Strategy Runtime (SRT) public contracts."""

from .algorithm import StrategyImplementation
from .calculation import CalculationScope, CalendarWindow, InputRange
from .charting import CHART_CONTEXT_VERSION, ChartRuntime, validate_chart_context
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
from .deployment import StrategyDeployment, deployment_inventory, load_strategy_deployment
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
    "CalculationScope",
    "CalendarWindow",
    "CHART_CONTEXT_VERSION",
    "ChartRuntime",
    "ExecutionCapabilities",
    "ExecutionOutcome",
    "ExecutionPlan",
    "ExecutionPolicy",
    "ExecutionState",
    "InputRange",
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
    "StrategyDeployment",
    "StrategyIdentity",
    "StrategyInit",
    "StrategyImplementation",
    "StrategyInstance",
    "StrategyRelease",
    "StrategyRuntime",
    "StrategyRuntimeError",
    "TradableWindow",
    "TradingPoint",
    "WindowExecutor",
    "canonical_sha256",
    "deployment_inventory",
    "load_strategy_deployment",
    "validate_chart_context",
]
