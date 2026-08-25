from __future__ import annotations

import ast
from pathlib import Path


PACKAGE = Path("src/czsc_trader")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            names.add(prefix + (node.module or ""))
    return names


def _has_main_guard(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        try:
            expression = ast.unparse(node.test)
        except Exception:
            continue
        if "__name__" in expression and "__main__" in expression:
            return True
    return False


def test_only_unified_cli_defines_command_parsing() -> None:
    violations: list[str] = []
    for path in PACKAGE.rglob("*.py"):
        relative = path.relative_to(PACKAGE).as_posix()
        if relative.startswith("cli/"):
            continue
        imports = _imports(path)
        if "argparse" in imports or _has_main_guard(path):
            violations.append(relative)
    assert violations == []


def test_research_does_not_depend_on_application_or_cli() -> None:
    violations: list[str] = []
    for path in (PACKAGE / "research").rglob("*.py"):
        for imported in _imports(path):
            if "czsc_trader.application" in imported or "czsc_trader.cli" in imported:
                violations.append(f"{path.name}: {imported}")
    assert violations == []


def test_legacy_scripts_are_removed() -> None:
    scripts = Path("scripts")
    assert not scripts.exists() or list(scripts.glob("*.py")) == []
