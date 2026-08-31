from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = Path(sys.executable).with_name(
    "czsc-trader.exe" if sys.platform == "win32" else "czsc-trader"
)
COMPARISON_KEYS = {
    "start",
    "end",
    "strategy_return",
    "buyhold_return",
    "return_difference",
    "strategy_sharpe",
    "buyhold_sharpe",
    "sharpe_difference",
    "strategy_max_drawdown",
    "buyhold_max_drawdown",
    "max_drawdown_difference",
}


def test_dataflows_package_imports_outside_the_checkout(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import dataflows; "
                "print(Path(dataflows.__file__).resolve())"
            ),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    module_path = Path(completed.stdout.strip())
    assert module_path.is_relative_to(REPO_ROOT / "packages" / "dataflows")


def test_installed_cli_runs_audited_backtest(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            str(CLI),
            "backtest",
            "run",
            "--symbol",
            "588080.SH",
            "--asset",
            "etf",
            "--start",
            "2026-01-01",
            "--end",
            "2026-08-21",
            "--outputs-root",
            str(tmp_path),
            "--repo-root",
            str(REPO_ROOT),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    output_dir = Path(payload["artifacts"]["output_dir"])
    full = payload["result"]["windows"]["full"]
    assert output_dir.name.startswith("588080_")
    assert output_dir.name.endswith("_BT01")
    assert payload["result"]["baseline"] == "baseline_20260826"
    assert set(full) == COMPARISON_KEYS
    assert full["strategy_return"] == pytest.approx(0.6197253904580182)
    assert full["return_difference"] == pytest.approx(
        full["strategy_return"] - full["buyhold_return"]
    )
    assert full["sharpe_difference"] == pytest.approx(
        full["strategy_sharpe"] - full["buyhold_sharpe"]
    )
    assert full["max_drawdown_difference"] == pytest.approx(
        full["strategy_max_drawdown"] - full["buyhold_max_drawdown"]
    )
    assert (output_dir / "audit.json").is_file()
    assert (output_dir / "report.md").is_file()
