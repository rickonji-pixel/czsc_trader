from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from czsc_trader.cli.main import main
from czsc_trader.research.handlers import registered_handlers


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


def test_czsc_factor_stability_handler_propagates_runner_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import czsc_trader.czsc_factor_stability_runner as runner

    expected = {"status": "PASS", "experiment_dir": str(tmp_path)}
    monkeypatch.setattr(
        runner,
        "run_czsc_factor_stability_experiment",
        lambda *args, **kwargs: expected,
    )
    handler = next(
        item
        for item in registered_handlers()
        if item.handler_id == "czsc_factor_stability_diagnostic"
    )
    context = SimpleNamespace(
        root=REPO_ROOT,
        raw_dir=REPO_ROOT / "data" / "raw",
        baseline_root=REPO_ROOT / "configs" / "rule_baselines",
    )

    assert handler.run(context, tmp_path) == expected


def test_czsc_route_family_handler_propagates_runner_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import czsc_trader.czsc_route_runner as runner

    expected = {"status": "FAIL", "experiment_dir": str(tmp_path)}
    monkeypatch.setattr(
        runner,
        "run_czsc_route_family",
        lambda *args, **kwargs: expected,
    )
    handler = next(
        item
        for item in registered_handlers()
        if item.handler_id == "czsc_route_family_diagnostic"
    )
    context = SimpleNamespace(
        root=REPO_ROOT,
        raw_dir=REPO_ROOT / "data" / "raw",
        baseline_root=REPO_ROOT / "configs" / "rule_baselines",
    )

    assert handler.run(context, tmp_path) == expected


def test_czsc_route_multiplicity_handler_propagates_runner_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import czsc_trader.czsc_multiplicity_runner as runner

    expected = {"status": "PASS", "experiment_dir": str(tmp_path)}
    monkeypatch.setattr(
        runner,
        "run_multiplicity_audit",
        lambda *args, **kwargs: expected,
    )
    handler = next(
        item
        for item in registered_handlers()
        if item.handler_id == "czsc_route_multiplicity_audit"
    )
    context = SimpleNamespace(
        root=REPO_ROOT,
        raw_dir=REPO_ROOT / "data" / "raw",
        baseline_root=REPO_ROOT / "configs" / "rule_baselines",
    )

    assert handler.run(context, tmp_path) == expected


def test_czsc_champion_condition_handler_propagates_runner_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import czsc_trader.czsc_champion_condition_runner as runner

    expected = {"status": "FAIL", "experiment_dir": str(tmp_path)}
    monkeypatch.setattr(
        runner,
        "run_champion_position_condition",
        lambda *args, **kwargs: expected,
    )
    handler = next(
        item
        for item in registered_handlers()
        if item.handler_id == "czsc_champion_position_condition"
    )
    context = SimpleNamespace(
        root=REPO_ROOT,
        raw_dir=REPO_ROOT / "data" / "raw",
        baseline_root=REPO_ROOT / "configs" / "rule_baselines",
    )

    assert handler.run(context, tmp_path) == expected


def invoke_cli(
    capsys: pytest.CaptureFixture[str],
    *arguments: object,
) -> tuple[int, str]:
    exit_code = main([str(argument) for argument in arguments])
    captured = capsys.readouterr()
    assert captured.err == ""
    return exit_code, captured.out


def json_result(
    capsys: pytest.CaptureFixture[str],
    *arguments: object,
    expected_exit: int = 0,
) -> dict[str, object]:
    exit_code, stdout = invoke_cli(capsys, *arguments)
    assert exit_code == expected_exit
    return json.loads(stdout)


def test_generic_window_uses_the_symbols_trading_dates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = json_result(
        capsys,
        "backtest",
        "run",
        "--symbol",
        "159516.SZ",
        "--asset",
        "etf",
        "--windows",
        REPO_ROOT / "configs" / "backtest_windows" / "2026.json",
        "--window",
        "2026FULL",
        "--outputs-root",
        tmp_path,
        "--repo-root",
        REPO_ROOT,
    )

    result = payload["result"]
    assert result["baseline"] == "baseline_20260826"
    assert "acceptance_status" not in result
    assert set(result["windows"]) == {"2026FULL"}
    window = result["windows"]["2026FULL"]
    assert set(window) == COMPARISON_KEYS
    assert window["start"] == "2026-01-05"
    assert window["end"] == "2026-08-28"


def test_archive_validation_checks_every_tracked_experiment(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = json_result(
        capsys,
        "archive",
        "validate",
        "--all",
        "--repo-root",
        REPO_ROOT,
    )

    expected = sorted(
        path.parent.name
        for path in (REPO_ROOT / "experiments").glob("*/experiment_manifest.json")
    )
    assert payload["status"] == "PASS"
    assert payload["result"]["validated_count"] == len(expected)
    assert payload["result"]["experiments"] == expected


def test_frozen_archive_cannot_run_in_place(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = json_result(
        capsys,
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


def test_experiment_replay_does_not_modify_the_source(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_dir = tmp_path / "0826_EX01_replay"
    source_manifest = REPO_ROOT / "experiments" / "0826_EX01" / "experiment_manifest.json"
    source_bytes = source_manifest.read_bytes()

    payload = json_result(
        capsys,
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


def test_output_contracts_without_process_startup(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = json_result(
        capsys,
        "baseline",
        "show",
        "--repo-root",
        REPO_ROOT,
        expected_exit=2,
    )
    assert payload["status"] == "FAIL"
    assert payload["command"] == "baseline.show"
    assert payload["error"]["code"] == "invalid_arguments"

    exit_code, stdout = invoke_cli(
        capsys,
        "baseline",
        "list",
        "--format",
        "text",
        "--repo-root",
        REPO_ROOT,
    )
    assert exit_code == 0
    assert stdout.startswith("PASS baseline.list\n")
    assert "baseline_20260826" in stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(stdout)
