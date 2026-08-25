from __future__ import annotations

from pathlib import Path

import pytest

from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.errors import UsageError


def _repository(root: Path) -> Path:
    (root / "src" / "czsc_trader").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    return root


def test_repository_context_discovers_root_from_nested_directory(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    nested = root / "one" / "two"
    nested.mkdir(parents=True)

    context = RepositoryContext.discover(nested)

    assert context.root == root.resolve()
    assert context.raw_dir == root.resolve() / "data" / "raw"
    assert context.baseline_root == root.resolve() / "configs" / "rule_baselines"
    assert context.experiments_root == root.resolve() / "experiments"
    assert context.outputs_root == root.resolve() / "outputs"


def test_repository_context_rejects_non_repository_explicit_root(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="repository root"):
        RepositoryContext.discover(tmp_path, explicit_root=tmp_path)
