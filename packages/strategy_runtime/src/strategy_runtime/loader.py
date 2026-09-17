"""Convention-based loading of immutable strategy runtime implementations."""

from __future__ import annotations

from importlib import import_module

from .errors import RuntimeCompatibilityError
from .models import StrategyRelease
from .protocols import ExecutableStrategy


class StrategyLoader:
    """Load one strategy without a central release switch or registry patch."""

    def load(self, release: StrategyRelease) -> ExecutableStrategy:
        module_name = (
            f"strategy_runtime.strategies."
            f"{release.strategy_family_id.lower()}_{release.version.lower()}"
        )
        class_name = f"{release.strategy_family_id}{release.version.upper()}"
        try:
            module = import_module(module_name)
            factory = getattr(module, class_name)
        except (ImportError, AttributeError) as exc:
            raise RuntimeCompatibilityError(
                f"strategy implementation is unavailable: {module_name}.{class_name}"
            ) from exc
        from_release = getattr(factory, "from_release", None)
        if not callable(from_release):
            raise RuntimeCompatibilityError(
                f"strategy implementation has no from_release factory: {class_name}"
            )
        strategy = from_release(release)
        if not isinstance(strategy, ExecutableStrategy):
            raise RuntimeCompatibilityError(
                f"loaded object does not implement ExecutableStrategy: {class_name}"
            )
        definition = strategy.definition
        if (
            definition.strategy_family_id != release.strategy_family_id
            or definition.version != release.version
            or definition.release_id != release.release_id
            or definition.release_hash != release.release_hash
        ):
            raise RuntimeCompatibilityError("loaded strategy identity differs from release")
        if (
            definition.implementation.module != module_name
            or definition.implementation.qualname != class_name
        ):
            raise RuntimeCompatibilityError(
                "loaded implementation identity differs from convention"
            )
        return strategy
