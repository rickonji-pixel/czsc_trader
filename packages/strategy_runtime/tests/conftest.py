from __future__ import annotations

from pathlib import Path
from importlib import import_module

import strategy_runtime


ROOT = Path(__file__).resolve().parents[3]

runtime_roots = sorted(
    (ROOT / "strategies").glob("S*/releases/v*/runtime/strategy_runtime")
)
for runtime_root in runtime_roots:
    value = str(runtime_root.resolve())
    if value not in strategy_runtime.__path__:
        strategy_runtime.__path__.append(value)

strategy_package = import_module("strategy_runtime.strategies")
strategy_package.__path__ = [
    str((runtime_root / "strategies").resolve()) for runtime_root in runtime_roots
]
