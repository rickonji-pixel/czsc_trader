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
