"""Convention-based loading of immutable strategy runtime implementations."""

from __future__ import annotations

from importlib import import_module
from importlib.resources import files
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
import sys

from .errors import RuntimeCompatibilityError
from .implementation_identity import implementation_sha256, load_runtime_binding
from .models import ImplementationRef, StrategyCandidate, StrategyRelease, canonical_sha256
from .algorithm import StrategyImplementation


# Capture frozen source closures before any strategy is imported. Re-reading a
# binding later must never relabel already-imported code with a new disk hash.
_PROCESS_IMPLEMENTATIONS = {
    path.stem: implementation_sha256(tuple(load_runtime_binding(path.stem)["source_files"]))
    for path in files("strategy_runtime").joinpath("bindings").iterdir()
    if path.name.endswith(".json")
}


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

    def _load_factory(
        self, release: StrategyRelease, source_root: Path | None = None,
    ):
        if not isinstance(release, StrategyRelease):
            raise RuntimeCompatibilityError("frozen loading requires a validated StrategyRelease")
        if "runtime" in release.payload:
            return self._declared_factory(release.payload, source_root=source_root)
        module_name = (
            f"strategy_runtime.strategies."
            f"{release.strategy_family_id.lower()}_{release.version.lower()}"
        )
        class_name = f"{release.strategy_family_id}{release.version.upper()}"
        binding = load_runtime_binding(release.release_id)
        actual = implementation_sha256(tuple(binding["source_files"]))
        if actual != binding["implementation_sha256"]:
            raise RuntimeCompatibilityError(
                f"frozen implementation differs from runtime binding: {release.release_id}"
            )
        if _PROCESS_IMPLEMENTATIONS.get(release.release_id) != actual:
            raise RuntimeCompatibilityError(
                "frozen implementation changed since process startup; use a fresh process"
            )
        try:
            module = import_module(module_name)
            factory = getattr(module, class_name)
        except (ImportError, AttributeError) as exc:
            raise RuntimeCompatibilityError(
                f"strategy implementation is unavailable: {module_name}.{class_name}"
            ) from exc
        if implementation_sha256(tuple(binding["source_files"])) != actual:
            raise RuntimeCompatibilityError("implementation source changed while loading")
        return module_name, class_name, factory

    @staticmethod
    def _declared_factory(
        payload: Mapping, *, source_root: Path | None = None,
    ):
        """Load the declared closure, with no convention fallback on invalid metadata."""
        descriptor = payload.get("runtime")
        expected = {"module", "qualname", "contract_version", "source_sha256", "source_files"}
        if not isinstance(descriptor, Mapping) or set(descriptor) != expected:
            raise RuntimeCompatibilityError("runtime implementation descriptor is incomplete")
        ref = ImplementationRef(**{key: descriptor[key] for key in expected - {"source_files"}})
        if ref.contract_version != 1:
            raise RuntimeCompatibilityError("unsupported implementation contract version")
        if not ref.module.startswith("strategy_runtime.strategies."):
            raise RuntimeCompatibilityError(
                "declared strategy must reside in the SRT strategies package"
            )
        if (
            not all(part.isidentifier() for part in ref.module.split("."))
            or not ref.qualname.isidentifier()
        ):
            raise RuntimeCompatibilityError("invalid declared Python implementation identity")
        source_files = descriptor["source_files"]
        if (
            not isinstance(source_files, (list, tuple))
            or not source_files
            or not all(isinstance(name, str) for name in source_files)
            or len(source_files) != len(set(source_files))
        ):
            raise RuntimeCompatibilityError(
                "runtime source closure must contain unique source paths"
            )
        for name in source_files:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or str(path) != name or "\\" in name or ":" in name:
                raise RuntimeCompatibilityError("runtime source closure contains an unsafe path")
        implementation_file = ref.module.removeprefix("strategy_runtime.").replace(".", "/") + ".py"
        if implementation_file not in source_files:
            raise RuntimeCompatibilityError(
                "runtime source closure omits the implementation module"
            )
        root = None if source_root is None else Path(source_root).resolve()
        if root is not None:
            if root.name != "strategy_runtime" or not (root / "strategies").is_dir():
                raise RuntimeCompatibilityError(
                    "candidate source root must be a strategy_runtime package directory"
                )
            import strategy_runtime
            import strategy_runtime.strategies

            for package, path in (
                (strategy_runtime, root),
                (strategy_runtime.strategies, root / "strategies"),
            ):
                package_path = str(path)
                if package_path not in package.__path__:
                    package.__path__.insert(0, package_path)
        actual = implementation_sha256(tuple(source_files), source_root=root)
        if actual != ref.source_sha256:
            raise RuntimeCompatibilityError(
                "declared implementation source hash differs from local code"
            )
        module = sys.modules.get(ref.module)
        if module is not None and getattr(module, "__srt_source_sha256__", None) != actual:
            raise RuntimeCompatibilityError(
                "declared implementation was already imported with an unverified or different "
                "source closure; use a fresh process"
            )
        previous_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            try:
                module = import_module(ref.module)
                factory = getattr(module, ref.qualname)
            except (ImportError, AttributeError) as exc:
                raise RuntimeCompatibilityError(
                    f"declared strategy is unavailable: {ref.module}.{ref.qualname}"
                ) from exc
        finally:
            sys.dont_write_bytecode = previous_bytecode
        if factory.__module__ != ref.module or factory.__qualname__ != ref.qualname:
            raise RuntimeCompatibilityError(
                "declared factory is an alias for another implementation"
            )
        if implementation_sha256(tuple(source_files), source_root=root) != actual:
            raise RuntimeCompatibilityError("implementation source changed while loading")
        module.__srt_source_sha256__ = actual
        return ref.module, ref.qualname, factory

    @staticmethod
    def _validate_declared_content(
        payload: Mapping, strategy: StrategyImplementation
    ) -> None:
        descriptor = payload["runtime"]
        definition = strategy.definition
        if definition.schema_version != 2:
            raise RuntimeCompatibilityError("declared implementations must use runtime schema 2")
        actual = definition.implementation
        for key in ("module", "qualname", "contract_version", "source_sha256"):
            if getattr(actual, key) != descriptor[key]:
                raise RuntimeCompatibilityError(
                    "runtime definition differs from declared implementation"
                )
        parameters = payload.get("parameters")
        if not isinstance(parameters, Mapping) or definition.parameters.sha256 != canonical_sha256(
            parameters
        ):
            raise RuntimeCompatibilityError(
                "runtime parameters differ from the supplied parameter set"
            )

    def load_candidate(self, candidate: StrategyCandidate) -> StrategyImplementation:
        """Run a parameterized candidate before submission without creating a frozen version."""
        if not isinstance(candidate, StrategyCandidate):
            raise RuntimeCompatibilityError("candidate loading requires a StrategyCandidate")
        module_name, class_name, factory = self._declared_factory(
            candidate.payload, source_root=candidate.source_root,
        )
        create = getattr(factory, "from_candidate", None)
        if not callable(create):
            raise RuntimeCompatibilityError(
                f"candidate implementation has no from_candidate factory: {class_name}"
            )
        strategy = create(candidate)
        if not isinstance(strategy, StrategyImplementation):
            raise RuntimeCompatibilityError(
                "candidate does not inherit StrategyImplementation"
            )
        definition = strategy.definition
        if (
            definition.identity_kind != "CANDIDATE"
            or definition.version is not None
            or definition.strategy_family_id != candidate.strategy_family_id
            or definition.candidate_id != candidate.candidate_id
            or definition.release_id != candidate.reference_id
            or definition.release_hash != candidate.runtime_identity_sha256
        ):
            raise RuntimeCompatibilityError("loaded runtime differs from candidate identity")
        self._validate_declared_content(candidate.payload, strategy)
        return strategy

    def _validate(
        self,
        release: StrategyRelease,
        strategy: StrategyImplementation,
        module_name: str,
        class_name: str,
    ) -> StrategyImplementation:
        if not isinstance(strategy, StrategyImplementation):
            raise RuntimeCompatibilityError(
                f"loaded object does not inherit StrategyImplementation: {class_name}"
            )
        definition = strategy.definition
        if (
            definition.strategy_family_id != release.strategy_family_id
            or definition.identity_kind != "RELEASE"
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
        if "runtime" in release.payload:
            self._validate_declared_content(release.payload, strategy)
            return strategy
        binding = load_runtime_binding(release.release_id)
        if binding.get("release_hash") != release.release_hash:
            raise RuntimeCompatibilityError("runtime binding release hash differs from release")
        source_files = tuple(binding["source_files"])
        actual_sha256 = implementation_sha256(source_files)
        if actual_sha256 != binding["implementation_sha256"]:
            raise RuntimeCompatibilityError(
                f"frozen implementation differs from runtime binding: {release.release_id}"
            )
        if _PROCESS_IMPLEMENTATIONS.get(release.release_id) != actual_sha256:
            raise RuntimeCompatibilityError(
                "frozen implementation changed since process startup; use a fresh process"
            )
        if definition.implementation.source_sha256 != actual_sha256:
            raise RuntimeCompatibilityError(
                "runtime definition implementation hash differs from its source closure"
            )
        return strategy

    def load(
        self, release: StrategyRelease, *, source_root: Path | None = None,
    ) -> StrategyImplementation:
        module_name, class_name, factory = self._load_factory(release, source_root)
        from_release = getattr(factory, "from_release", None)
        if not callable(from_release):
            raise RuntimeCompatibilityError(
                f"strategy implementation has no from_release factory: {class_name}"
            )
        strategy = from_release(release)
        return self._validate(release, strategy, module_name, class_name)

    def load_for_symbol(
        self, release: StrategyRelease, symbol: str, *, source_root: Path | None = None,
    ) -> StrategyImplementation:
        """Bind a formula-compatible release to one explicit deployment symbol.

        A strategy must opt in by implementing ``from_release_for_symbol``.
        Symbol-specific mechanisms therefore fail closed instead of silently
        replaying their frozen source instrument against another price series.
        """

        module_name, class_name, factory = self._load_factory(release, source_root)
        binder = getattr(factory, "from_release_for_symbol", None)
        if not callable(binder):
            strategy = self.load(release, source_root=source_root)
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
