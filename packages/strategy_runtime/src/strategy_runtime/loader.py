"""Convention-based loading of immutable strategy runtime implementations."""

from __future__ import annotations

from importlib import import_module

from .errors import RuntimeCompatibilityError
from .implementation_identity import implementation_sha256, load_runtime_binding
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
        binding = load_runtime_binding(release.release_id)
        if binding.get("release_hash") != release.release_hash:
            raise RuntimeCompatibilityError("runtime binding release hash differs from release")
        source_files = tuple(binding["source_files"])
        actual_sha256 = implementation_sha256(source_files)
        if actual_sha256 != binding["implementation_sha256"]:
            raise RuntimeCompatibilityError(
                f"frozen implementation differs from runtime binding: {release.release_id}"
            )
        if definition.implementation.source_sha256 != actual_sha256:
            raise RuntimeCompatibilityError(
                "runtime definition implementation hash differs from its source closure"
            )
        return strategy
