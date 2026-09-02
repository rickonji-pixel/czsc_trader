from __future__ import annotations

import json
from pathlib import Path
import socket

import pytest


class OnceEngine:
    def __init__(self):
        self.closed = False

    def refresh(self):
        return {"symbol": "588080.SH", "paused": False}

    def close(self):
        self.closed = True


def test_pte_once_refreshes_and_emits_one_json_document(tmp_path: Path, capsys) -> None:
    from paper_trading_engine.cli import main

    engine = OnceEngine()
    code = main(
        ["once", "--repo-root", str(tmp_path), "--position-size", "50000"],
        engine_factory=lambda args: engine,
    )

    assert code == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out) == {
        "status": "PASS",
        "command": "pte.once",
        "result": {"symbol": "588080.SH", "paused": False},
    }
    assert len(output.out.splitlines()) == 1
    assert engine.closed is True


def test_pte_serve_defaults_to_localhost_and_runtime_database(tmp_path: Path) -> None:
    from paper_trading_engine.cli import build_parser

    args = build_parser().parse_args(
        ["serve", "--repo-root", str(tmp_path), "--position-size", "50000"]
    )

    assert args.host == "127.0.0.1"
    assert args.port == 8080
    assert args.database == tmp_path.resolve() / "state" / "paper_trading" / "runtime.db"
    assert args.data_dir == tmp_path.resolve() / "state" / "paper_trading" / "data"
    assert args.order_interval == 5
    assert args.account_interval == 60
    assert args.data_refresh_time == "19:00"


def test_probe_port_reports_an_explicit_conflict() -> None:
    from paper_trading_engine.cli import PortUnavailableError, probe_port

    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    port = occupied.getsockname()[1]
    try:
        with pytest.raises(PortUnavailableError, match=f"127.0.0.1:{port}.*already in use"):
            probe_port("127.0.0.1", port)
    finally:
        occupied.close()


def test_account_commands_parse_with_runtime_defaults(tmp_path: Path) -> None:
    from paper_trading_engine.cli import build_parser

    create = build_parser().parse_args([
        "account", "create", "--repo-root", str(tmp_path),
        "--account-id", "range-2", "--name", "Range 2",
        "--baseline", "baseline_20260903", "--initial-cash", "1000000",
    ])
    pause = build_parser().parse_args([
        "account", "pause", "--repo-root", str(tmp_path), "--account-id", "range-2",
    ])

    assert create.database == tmp_path.resolve() / "state" / "paper_trading" / "runtime.db"
    assert create.initial_cash == "1000000"
    assert pause.account_id == "range-2"


def test_account_create_list_pause_resume_without_broker(tmp_path: Path, capsys, monkeypatch) -> None:
    from paper_trading_engine import cli

    monkeypatch.setattr(cli, "_validate_baseline", lambda *args: {
        "version": "baseline_20260903", "sha256": "a" * 64,
    })
    base = ["--repo-root", str(tmp_path)]
    assert cli.main(["account", "create", *base, "--account-id", "range-2", "--name", "Range 2",
                     "--baseline", "baseline_20260903", "--initial-cash", "1000000"]) == 0
    assert cli.main(["account", "pause", *base, "--account-id", "range-2"]) == 0
    assert cli.main(["account", "resume", *base, "--account-id", "range-2"]) == 0
    assert cli.main(["account", "list", *base]) == 0
    documents = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert documents[0]["result"]["baseline_sha256"] == "a" * 64
    assert documents[1]["result"]["paused"] == 1
    assert documents[2]["result"]["paused"] == 0
    assert documents[3]["result"][0]["account_id"] == "range-2"
