from __future__ import annotations

import json
from pathlib import Path
import shutil

import pandas as pd
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
    raw_dir = root / "data" / "raw"
    raw_dir.mkdir(parents=True)
    for source in (REPO_ROOT / "data" / "raw").glob("588080*"):
        shutil.copy2(source, raw_dir / source.name)
    (root / "experiments").mkdir()
    (root / "outputs").mkdir()
    return root


def vendor_frame(rows: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Date": timestamp,
                "Open": close,
                "High": close,
                "Low": close,
                "Close": close,
                "Volume": 1000.0,
                "Amount": close * 1000.0,
            }
            for timestamp, close in rows
        ]
    )


def invoke_main(arguments: list[str], capsys) -> dict:
    from czsc_trader.cli.main import main

    exit_code = main(arguments)
    output = capsys.readouterr()
    assert output.err == ""
    payload = json.loads(output.out)
    assert exit_code == 0
    assert payload["status"] == "PASS"
    return payload


def invoke_main_failure(arguments: list[str], capsys) -> dict:
    from czsc_trader.cli.main import main

    exit_code = main(arguments)
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert exit_code != 0
    assert payload["status"] == "FAIL"
    return payload
