"""Factory for immutable strategy instances."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .contracts import StrategyIdentity, TradableWindow
from .loader import StrategyLoader
from .models import ExecutionPolicy, StrategyCandidate, StrategyRelease
from .strategy import StrategyInstance


def _definition_symbol(definition) -> str:
    subjects = {
        str(requirement.subject).upper()
        for requirement in definition.inputs.requirements
        if requirement.subject
        and requirement.dataset.startswith(("etf.", "stock."))
        and re.fullmatch(r"\d{6}\.(?:SH|SZ)", str(requirement.subject).upper())
    }
    if len(subjects) != 1:
        raise ValueError("strategy must declare exactly one A-share instrument")
    return next(iter(subjects))


@dataclass(frozen=True, slots=True)
class StrategyInit:
    source: StrategyRelease | StrategyCandidate
    tradable_window: TradableWindow
    data_dir: Path
    symbol: str | None = None
    execution_policy: ExecutionPolicy | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_dir", Path(self.data_dir).resolve())


class StrategyRuntime:
    """Create strategy instances; all running behavior belongs to the instance."""

    def __init__(self) -> None:
        self._loader = StrategyLoader()

    def _load(
        self,
        source: StrategyRelease | StrategyCandidate,
        symbol: str | None,
    ):
        if isinstance(source, StrategyRelease):
            return (
                self._loader.load_for_symbol(source, symbol)
                if symbol is not None
                else self._loader.load(source)
            )
        if symbol is not None:
            raise ValueError("candidate strategy does not support symbol rebinding")
        return self._loader.load_candidate(source)

    def describe(
        self,
        source: StrategyRelease | StrategyCandidate,
        *,
        symbol: str | None = None,
    ):
        """Return the validated frozen definition without creating an instance."""

        return self._load(source, symbol).definition

    def create(self, request: StrategyInit) -> StrategyInstance:
        algorithm = self._load(request.source, request.symbol)
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
            tradable_window=request.tradable_window,
            data_dir=request.data_dir,
            execution_policy=request.execution_policy or definition.execution,
        )
