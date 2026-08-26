from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
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
CLI = Path(sys.executable).with_name(
    "czsc-trader.exe" if sys.platform == "win32" else "czsc-trader"
)


def run_cli(*arguments: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CLI), *(str(argument) for argument in arguments)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def json_result(*arguments: object, expected_exit: int = 0) -> dict[str, object]:
    completed = run_cli(*arguments)
    assert completed.returncode == expected_exit, completed.stdout + completed.stderr
    assert completed.stderr == ""
    return json.loads(completed.stdout)


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


def test_data_validate_reads_the_tracked_market_dataset() -> None:
    payload = json_result(
        "data",
        "validate",
        "--symbol",
        "588080.SH",
        "--repo-root",
        REPO_ROOT,
    )

    assert payload["status"] == "PASS"
    assert payload["result"] == {
        "symbol": "588080.SH",
        "asset_type": "etf",
        "requested_start": "2020-01-01",
        "requested_end": "2026-08-26",
        "validation_status": "PASS",
        "frequencies": ["30m", "daily", "weekly"],
        "file_count": 21,
    }


def test_baseline_commands_expose_and_verify_the_active_baseline() -> None:
    listed = json_result("baseline", "list", "--repo-root", REPO_ROOT)
    shown = json_result(
        "baseline",
        "show",
        "--version",
        "baseline_20260826",
        "--symbol",
        "588080.SH",
        "--repo-root",
        REPO_ROOT,
    )
    validated = json_result(
        "baseline",
        "validate",
        "--version",
        "baseline_20260826",
        "--symbol",
        "588080.SH",
        "--repo-root",
        REPO_ROOT,
    )

    assert listed["result"]["latest"] == "baseline_20260826"
    assert shown["result"]["strategy"] == "czsc_four_layer"
    assert validated["status"] == "PASS"
    assert validated["result"]["sha256"] == (
        "fc22ca5a973f77faf528cdb3efba4900163e18fe0c08cf79234d79ef22f5c822"
    )


def test_backtest_run_publishes_audited_artifacts(tmp_path: Path) -> None:
    payload = json_result(
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
        tmp_path,
        "--repo-root",
        REPO_ROOT,
    )

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


def test_backtest_window_resolves_first_and_last_trading_day_in_year(
    tmp_path: Path,
) -> None:
    payload = json_result(
        "backtest",
        "run",
        "--symbol",
        "588080.SH",
        "--asset",
        "etf",
        "--windows",
        REPO_ROOT / "configs" / "backtest_windows" / "588080_2026.json",
        "--window",
        "2026FULL",
        "--outputs-root",
        tmp_path,
        "--repo-root",
        REPO_ROOT,
    )

    result = payload["result"]
    assert "acceptance_status" not in result
    assert set(result["windows"]) == {"2026FULL"}
    window = result["windows"]["2026FULL"]
    assert set(window) == COMPARISON_KEYS
    assert window["start"] == "2026-01-05"
    assert window["end"] == "2026-08-26"


def test_archive_validate_checks_every_tracked_experiment() -> None:
    payload = json_result("archive", "validate", "--all", "--repo-root", REPO_ROOT)

    assert payload["status"] == "PASS"
    assert payload["result"]["validated_count"] == 15
    assert payload["result"]["experiments"] == [
        "0824_EX01",
        "0824_EX02",
        "0824_EX03",
        "0824_EX04",
        "0824_EX05",
        "0824_EX06",
        "0824_EX07",
        "0824_EX08",
        "0825_EX01",
        "0825_EX02",
        "0825_EX03",
        "0825_EX04",
        "0825_EX05",
        "0825_EX06",
        "0826_EX01",
    ]


def test_experiment_run_rejects_a_frozen_archive() -> None:
    payload = json_result(
        "experiment",
        "run",
        "--dir",
        "experiments/0824_EX04",
        "--repo-root",
        REPO_ROOT,
        expected_exit=4,
    )

    assert payload["status"] == "FAIL"
    assert payload["error"]["code"] == "frozen_experiment_run_forbidden"


def test_experiment_replay_is_isolated_from_the_source_archive(tmp_path: Path) -> None:
    output_dir = tmp_path / "0826_EX01_replay"
    source_manifest = REPO_ROOT / "experiments" / "0826_EX01" / "experiment_manifest.json"
    source_bytes = source_manifest.read_bytes()

    payload = json_result(
        "experiment",
        "replay",
        "--dir",
        "experiments/0826_EX01",
        "--output",
        output_dir,
        "--repo-root",
        REPO_ROOT,
    )

    replay_manifest = json.loads(
        (output_dir / "experiment_manifest.json").read_text(encoding="utf-8")
    )
    assert payload["status"] == "PASS"
    assert replay_manifest["run_type"] == "experiment_replay"
    assert replay_manifest["source_experiment"] == "0826_EX01"
    assert source_manifest.read_bytes() == source_bytes


def test_invalid_arguments_return_the_json_error_contract() -> None:
    payload = json_result(
        "baseline",
        "show",
        "--repo-root",
        REPO_ROOT,
        expected_exit=2,
    )

    assert payload["status"] == "FAIL"
    assert payload["command"] == "baseline.show"
    assert payload["error"]["code"] == "invalid_arguments"


def test_text_format_is_human_readable() -> None:
    completed = run_cli(
        "baseline",
        "list",
        "--format",
        "text",
        "--repo-root",
        REPO_ROOT,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert completed.stdout.startswith("PASS baseline.list\n")
    assert "baseline_20260826" in completed.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(completed.stdout)
