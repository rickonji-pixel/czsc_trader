from __future__ import annotations

from pathlib import Path
import shutil

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def functional_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "czsc_trader").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[project]\nname='czsc-trader-functional-test'\nversion='0.1.0'\n",
        encoding="utf-8",
    )
    shutil.copytree(REPO_ROOT / "configs", root / "configs")
    shutil.copytree(REPO_ROOT / "strategies", root / "strategies")
    raw_dir = root / "data" / "raw"
    raw_dir.mkdir(parents=True)
    for source in (REPO_ROOT / "data" / "raw").glob("588080*"):
        shutil.copy2(source, raw_dir / source.name)
    shutil.copytree(raw_dir, root / "data" / "backtest")
    for relative in (
        Path("0824_EX04/artifacts/frozen_challenger.json"),
        Path("0901_EX20/artifacts/frozen_challenger.json"),
        Path("0902_EX02/artifacts/frozen_execution_policy.json"),
        Path("0903_EX06/artifacts/frozen_challenger.json"),
    ):
        source = REPO_ROOT / "experiments" / relative
        if source.is_file():
            destination = root / "experiments" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    (root / "experiments").mkdir(exist_ok=True)
    (root / "outputs").mkdir()
    return root
