from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest


def test_cli_data_publisher_uses_runtime_directory_and_machine_contract(tmp_path: Path) -> None:
    from paper_trading_engine.data_publisher import CliDataPublisher

    calls = []

    def runner(arguments, **kwargs):
        calls.append((arguments, kwargs))
        payload = {"status": "PASS", "command": "data.prepare", "result": {"symbol": "588080.SH"}}
        return CompletedProcess(arguments, 0, json.dumps(payload), "")

    publisher = CliDataPublisher(
        executable=Path("C:/tools/czsc-trader.exe"),
        repo_root=tmp_path,
        data_dir=tmp_path / "state" / "paper_trading" / "data",
        symbol="588080.SH",
        asset="etf",
        start_date="2020-01-01",
        runner=runner,
    )
    result = publisher.publish("2026-09-02")

    assert result["symbol"] == "588080.SH"
    arguments, options = calls[0]
    assert arguments[-4:] == [
        "--data-dir",
        str((tmp_path / "state" / "paper_trading" / "data").resolve()),
        "--format",
        "json",
    ]
    assert ["--end", "2026-09-02"] == arguments[arguments.index("--end"):arguments.index("--end") + 2]
    assert options["shell"] is False


def test_seed_runtime_data_copies_only_selected_symbol_once(tmp_path: Path) -> None:
    from paper_trading_engine.data_publisher import seed_runtime_data

    source, target = tmp_path / "raw", tmp_path / "runtime"
    source.mkdir()
    (source / "588080_manifest.json").write_text("v1", encoding="utf-8")
    (source / "588080_daily_2026.csv").write_text("rows", encoding="utf-8")
    (source / "159352_manifest.json").write_text("other", encoding="utf-8")

    seed_runtime_data(source, target, "588080.SH")
    (source / "588080_manifest.json").write_text("v2", encoding="utf-8")
    seed_runtime_data(source, target, "588080.SH")

    assert sorted(path.name for path in target.iterdir()) == [
        "588080_daily_2026.csv", "588080_manifest.json"
    ]
    assert (target / "588080_manifest.json").read_text(encoding="utf-8") == "v1"


def test_cli_data_publisher_surfaces_machine_error_message(tmp_path: Path) -> None:
    from paper_trading_engine.data_publisher import CliDataPublisher, DataPublicationError

    payload = {
        "status": "FAIL",
        "error": {"message": "30m/daily reconciliation: trade dates differ"},
    }
    publisher = CliDataPublisher(
        executable=Path("czsc-trader"), repo_root=tmp_path, data_dir=tmp_path / "data",
        symbol="588080.SH", asset="etf", start_date="2020-01-01",
        runner=lambda arguments, **kwargs: CompletedProcess(
            arguments, 3, json.dumps(payload), ""
        ),
    )

    with pytest.raises(DataPublicationError, match="trade dates differ"):
        publisher.publish("2026-09-02")
