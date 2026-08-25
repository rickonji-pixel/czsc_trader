from __future__ import annotations

import json
from pathlib import Path

from czsc_trader.application.errors import ValidationError
from czsc_trader.application.results import CommandResult
from czsc_trader.cli.main import main
from czsc_trader.cli.output import write_error, write_result


def test_result_writer_emits_one_stable_json_document(capsys) -> None:
    write_result(
        CommandResult(
            status="PASS",
            command="baseline.list",
            result={"count": 2},
            artifacts={"output_dir": "outputs/example"},
            warnings=("observed history",),
        )
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload == {
        "status": "PASS",
        "command": "baseline.list",
        "result": {"count": 2},
        "artifacts": {"output_dir": "outputs/example"},
        "warnings": ["observed history"],
    }
    assert captured.out.count("\n") == 1


def test_error_writer_preserves_stable_code_and_context(capsys) -> None:
    error = ValidationError(
        "baseline_hash_mismatch",
        "baseline digest does not match registry",
        context={"version": "baseline_20260826"},
    )

    exit_code = write_error("baseline.validate", error)

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert payload["status"] == "FAIL"
    assert payload["error"] == {
        "code": "baseline_hash_mismatch",
        "message": "baseline digest does not match registry",
        "context": {"version": "baseline_20260826"},
    }


def test_baseline_list_command_returns_parseable_json(capsys) -> None:
    exit_code = main(["baseline", "list", "--repo-root", "."])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert captured.err == ""
    assert payload["command"] == "baseline.list"
    assert payload["result"]["latest"] == "baseline_20260826"


def test_command_failure_is_a_stable_json_error(capsys, tmp_path) -> None:
    exit_code = main(
        ["baseline", "list", "--repo-root", str(tmp_path / "not-a-repository")]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["status"] == "FAIL"
    assert payload["error"]["code"] == "repository_root_not_found"


def test_backtest_command_routes_through_application_service(capsys, tmp_path) -> None:
    exit_code = main(
        [
            "backtest",
            "run",
            "--symbol",
            "588080.SH",
            "--asset",
            "etf",
            "--start",
            "2026-01-01",
            "--end",
            "2026-01-30",
            "--outputs-root",
            str(tmp_path),
            "--repo-root",
            ".",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["command"] == "backtest.run"
    assert payload["result"]["baseline"] == "baseline_20260826"
    assert Path(payload["artifacts"]["output_dir"]).is_dir()
