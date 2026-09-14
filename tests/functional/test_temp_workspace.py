from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

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


def test_windows_temporary_directory_preserves_inherited_acl(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows ACL regression")

    created = create_temporary_directory(
        tmp_path,
        "acl-regression",
        repository_root=tmp_path,
    )
    result = subprocess.run(
        ["icacls", str(created)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "(I)" in result.stdout


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


def test_pytest_temporary_path_stays_inside_repository(tmp_path: Path) -> None:
    marker = tmp_path / "marker.txt"
    marker.write_text("ok", encoding="utf-8")

    assert marker.read_text(encoding="utf-8") == "ok"
    assert tmp_path.parent.parent.name == "pytest"
    assert tmp_path.parent.parent.parent.name == ".tmp"


def test_temporary_namespace_rejects_path_traversal(functional_repo: Path) -> None:
    with pytest.raises(ValueError, match="temporary namespace"):
        create_temporary_directory(functional_repo, "../outside")
    with pytest.raises(ValueError, match="temporary prefix"):
        create_temporary_directory(functional_repo, "backtest", prefix="../outside")

    assert temporary_root(functional_repo) == functional_repo / ".tmp"
