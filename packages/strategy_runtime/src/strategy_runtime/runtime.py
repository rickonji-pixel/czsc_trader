"""Factory for immutable strategy instances."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import StrategyIdentity, TradableWindow
from .loader import StrategyLoader
from .models import ExecutionPolicy, StrategyCandidate, StrategyRelease
from .publication_store import _definition_symbol
from .strategy import StrategyInstance


@dataclass(frozen=True, slots=True)
class StrategyInit:
    source: StrategyRelease | StrategyCandidate
    tradable_window: TradableWindow
    symbol: str | None = None
    execution_policy: ExecutionPolicy | None = None


class StrategyRuntime:
    """Create strategy instances; all running behavior belongs to the instance."""

    def __init__(self) -> None:
        self._loader = StrategyLoader()

    def describe(
        self,
        source: StrategyRelease | StrategyCandidate,
        *,
        symbol: str | None = None,
    ):
        """Return the validated frozen definition without creating an instance."""

        if isinstance(source, StrategyRelease):
            algorithm = (
                self._loader.load_for_symbol(source, symbol)
                if symbol is not None
                else self._loader.load(source)
            )
        else:
            if symbol is not None:
                raise ValueError("candidate strategy does not support symbol rebinding")
            algorithm = self._loader.load_candidate(source)
        return algorithm.definition

    def create(self, request: StrategyInit) -> StrategyInstance:
        source = request.source
        if isinstance(source, StrategyRelease):
            algorithm = (
                self._loader.load_for_symbol(source, request.symbol)
                if request.symbol is not None
                else self._loader.load(source)
            )
        else:
            if request.symbol is not None:
                raise ValueError("candidate strategy does not support symbol rebinding")
            algorithm = self._loader.load_candidate(source)
        definition = algorithm.definition
        if (
            request.execution_policy is not None
            and request.execution_policy.policy_type != definition.execution.policy_type
        ):
            raise ValueError("execution policy override must preserve the strategy policy type")
        symbol = _definition_symbol(definition)
        identity = StrategyIdentity(
            strategy_id=definition.strategy_family_id,
            reference_id=definition.release_id,
            release_hash=definition.release_hash,
            runtime_sha256=definition.runtime_sha256,
            symbol=symbol,
        )
        return StrategyInstance(
            algorithm=algorithm,
            identity=identity,
            window=request.tradable_window,
            execution_policy=request.execution_policy or definition.execution,
        )
