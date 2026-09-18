"""Convention-based loading of immutable strategy runtime implementations."""

from __future__ import annotations

from importlib import import_module
from collections.abc import Mapping

from .errors import RuntimeCompatibilityError
from .implementation_identity import implementation_sha256, load_runtime_binding
from .models import StrategyRelease
from .protocols import ExecutableStrategy


def _release_symbol(release: StrategyRelease) -> str | None:
    payload = release.payload
    rule = payload.get("rule")
    if isinstance(rule, Mapping):
        execution = rule.get("execution")
        if isinstance(execution, Mapping):
            instrument = execution.get("instrument")
            if isinstance(instrument, Mapping) and isinstance(instrument.get("symbol"), str):
                return str(instrument["symbol"]).upper()
        if isinstance(rule.get("symbol"), str):
            return str(rule["symbol"]).upper()
    if isinstance(payload.get("symbol"), str):
        return str(payload["symbol"]).upper()
    return None


class StrategyLoader:
    """Load one strategy without a central release switch or registry patch."""

    def _load_factory(self, release: StrategyRelease):
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
        return module_name, class_name, factory

    def _validate(
        self,
        release: StrategyRelease,
        strategy: ExecutableStrategy,
        module_name: str,
        class_name: str,
    ) -> ExecutableStrategy:
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

    def load(self, release: StrategyRelease) -> ExecutableStrategy:
        module_name, class_name, factory = self._load_factory(release)
        from_release = getattr(factory, "from_release", None)
        if not callable(from_release):
            raise RuntimeCompatibilityError(
                f"strategy implementation has no from_release factory: {class_name}"
            )
        strategy = from_release(release)
        return self._validate(release, strategy, module_name, class_name)

    def load_for_symbol(
        self, release: StrategyRelease, symbol: str
    ) -> ExecutableStrategy:
        """Bind a formula-compatible release to one explicit deployment symbol.

        A strategy must opt in by implementing ``from_release_for_symbol``.
        Symbol-specific mechanisms therefore fail closed instead of silently
        replaying their frozen source instrument against another price series.
        """

        module_name, class_name, factory = self._load_factory(release)
        binder = getattr(factory, "from_release_for_symbol", None)
        if not callable(binder):
            strategy = self.load(release)
            subjects = {
                item.subject.upper()
                for item in strategy.definition.inputs.requirements
                if item.subject and item.dataset.startswith("etf.")
            }
            if subjects == {symbol.upper()} or _release_symbol(release) == symbol.upper():
                return strategy
            raise RuntimeCompatibilityError(
                f"{release.release_id} does not support deployment symbol rebinding"
            )
        strategy = binder(release, symbol.upper())
        return self._validate(release, strategy, module_name, class_name)
