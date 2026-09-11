from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from czsc_trader.temp_workspace import create_temporary_directory, temporary_root


def test_repository_temporary_directories_are_namespaced(functional_repo: Path) -> None:
    first = create_temporary_directory(
        functional_repo / "outputs", "backtest", prefix="run-"
    )
    second = create_temporary_directory(
        functional_repo / "data" / "raw", "market-data", prefix="588080-"
    )

    assert first.parent == functional_repo / ".tmp" / "backtest"
    assert second.parent == functional_repo / ".tmp" / "market-data"
    assert first != second
    shutil.rmtree(first)
    shutil.rmtree(second)


def test_explicit_repository_root_controls_external_anchor(
    functional_repo: Path, tmp_path: Path
) -> None:
    created = create_temporary_directory(
        tmp_path / "external-output",
        "evaluation",
        repository_root=functional_repo,
    )

    assert created.parent == functional_repo / ".tmp" / "evaluation"
    shutil.rmtree(created)


def test_temporary_namespace_rejects_path_traversal(functional_repo: Path) -> None:
    with pytest.raises(ValueError, match="temporary namespace"):
        create_temporary_directory(functional_repo, "../outside")
    with pytest.raises(ValueError, match="temporary prefix"):
        create_temporary_directory(functional_repo, "backtest", prefix="../outside")

    assert temporary_root(functional_repo) == functional_repo / ".tmp"
