"""Stable SRT domain errors."""


class StrategyRuntimeError(Exception):
    """Base class for SRT errors."""


class RuntimeContractError(StrategyRuntimeError, ValueError):
    """A runtime object violates the public SRT contract."""


class RuntimeCompatibilityError(StrategyRuntimeError):
    """A deployment host cannot satisfy the strategy capabilities."""


class RuntimeExecutionError(StrategyRuntimeError):
    """A strategy runtime operation failed after contract validation."""
