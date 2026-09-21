"""Factory for immutable strategy instances."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import DecisionWindow, StrategyIdentity
from .loader import StrategyLoader
from .models import StrategyCandidate, StrategyRelease
from .publication_store import _definition_symbol
from .strategy import StrategyInstance


@dataclass(frozen=True, slots=True)
class StrategyInit:
    source: StrategyRelease | StrategyCandidate
    decision_window: DecisionWindow
    symbol: str | None = None


class StrategyRuntime:
    """Create strategy instances; all running behavior belongs to the instance."""

    def __init__(self) -> None:
        self._loader = StrategyLoader()

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
            window=request.decision_window,
        )
