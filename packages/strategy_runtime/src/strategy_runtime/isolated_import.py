"""Load one authenticated strategy closure without polluting platform modules."""

from __future__ import annotations

from hashlib import sha256
from importlib import import_module
from importlib.machinery import ModuleSpec
from pathlib import Path, PurePosixPath
from threading import RLock
from types import ModuleType
import sys

from .errors import RuntimeCompatibilityError


_IMPORT_GUARD = RLock()
_PLATFORM_MODULES = (
    "algorithm",
    "calculation",
    "contracts",
    "errors",
    "execution_rules",
    "implementation_identity",
    "models",
    "observation",
    "signals",
)


def _package(name: str, root: Path) -> ModuleType:
    package = ModuleType(name)
    package.__package__ = name
    package.__path__ = [str(root)]
    package.__spec__ = ModuleSpec(name, loader=None, is_package=True)
    return package


def load_closure_module(
    module_name: str,
    *,
    source_root: Path,
    source_files: tuple[str, ...],
    source_sha256: str,
    marker: str,
) -> ModuleType:
    """Import a release module under an isolated dependency namespace.

    The returned module keeps its governed canonical name for runtime identity,
    while relative imports resolve inside a closure-specific package. Platform
    modules are aliased only when the release did not freeze its own copy.
    """

    root = Path(source_root).resolve()
    if not module_name.startswith("strategy_runtime."):
        raise RuntimeCompatibilityError("isolated module must belong to strategy_runtime")
    relative_module = module_name.removeprefix("strategy_runtime.")
    implementation_file = PurePosixPath(relative_module.replace(".", "/") + ".py")
    expected_file = root.joinpath(*implementation_file.parts).resolve()
    if not expected_file.is_file():
        raise RuntimeCompatibilityError(
            f"isolated implementation is unavailable: {module_name}"
        )
    root_identity = str(root)
    namespace_digest = sha256(
        f"{source_sha256}\0{root_identity}".encode("utf-8")
    ).hexdigest()[:24]
    namespace = f"strategy_runtime._closure_{namespace_digest}"
    isolated_name = f"{namespace}.{relative_module}"
    marker_name = f"__{marker}_sha256__"
    root_marker_name = f"__{marker}_root__"

    with _IMPORT_GUARD:
        current = sys.modules.get(module_name)
        if current is not None:
            current_sha = getattr(current, marker_name, None)
            current_root = getattr(current, root_marker_name, None)
            if current_sha == source_sha256 and current_root == root_identity:
                return current
            if current_sha is None:
                raise RuntimeCompatibilityError(
                    f"{module_name} was already imported without closure authentication"
                )
            if current_root == root_identity and current_sha != source_sha256:
                raise RuntimeCompatibilityError(
                    f"{module_name} source changed after import; use a fresh process"
                )

        before = set(sys.modules)
        previous_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            sys.modules.setdefault(namespace, _package(namespace, root))
            frozen_top_level = {
                path.parts[0][:-3]
                for value in source_files
                if len((path := PurePosixPath(value)).parts) == 1
                and path.suffix == ".py"
            }
            for leaf in _PLATFORM_MODULES:
                if leaf not in frozen_top_level:
                    sys.modules.setdefault(
                        f"{namespace}.{leaf}",
                        import_module(f"strategy_runtime.{leaf}"),
                    )
            module = import_module(isolated_name)
        except Exception:
            for name in tuple(sys.modules):
                if name not in before and (name == namespace or name.startswith(namespace + ".")):
                    sys.modules.pop(name, None)
            raise
        finally:
            sys.dont_write_bytecode = previous_bytecode

        module_file = getattr(module, "__file__", None)
        if module_file is None or Path(module_file).resolve() != expected_file:
            raise RuntimeCompatibilityError(
                f"{module_name} loaded from outside its authenticated closure"
            )
        module.__name__ = module_name
        setattr(module, marker_name, source_sha256)
        setattr(module, root_marker_name, root_identity)
        sys.modules[module_name] = module
        return module
