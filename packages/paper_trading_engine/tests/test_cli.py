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
        "--strategy", "S001", "--strategy-version", "v1",
    ])
    pause = build_parser().parse_args([
        "account", "pause", "--repo-root", str(tmp_path), "--account-id", "range-2",
    ])

    assert create.database == tmp_path.resolve() / "state" / "paper_trading" / "runtime.db"
    assert create.initial_cash == "100000"
    assert pause.account_id == "range-2"


def test_account_create_list_pause_resume_without_broker(tmp_path: Path, capsys, monkeypatch) -> None:
    from paper_trading_engine import cli

    monkeypatch.setattr(cli, "_validate_strategy", lambda *args: {
        "strategy_id": "S001", "name": "综合基线策略", "version": "v1",
        "release_id": "S001-v1", "release_hash": "a" * 64,
        "qualification": "PAPER_READY", "strategy_payload": {},
    })
    base = ["--repo-root", str(tmp_path)]
    assert cli.main(["account", "create", *base, "--account-id", "range-2", "--name", "Range 2",
                     "--strategy", "S001", "--strategy-version", "v1",
                     "--initial-cash", "1000000"]) == 0
    assert cli.main(["account", "pause", *base, "--account-id", "range-2"]) == 0
    assert cli.main(["account", "resume", *base, "--account-id", "range-2"]) == 0
    assert cli.main(["account", "list", *base]) == 0
    documents = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert documents[0]["result"]["release_hash"] == "a" * 64
    assert documents[1]["result"]["paused"] == 1
    assert documents[2]["result"]["paused"] == 0
    assert documents[3]["result"][0]["account_id"] == "range-2"


def test_channel_bind_strategy_persists_validated_release(tmp_path: Path, capsys, monkeypatch) -> None:
    from paper_trading_engine import cli
    from paper_trading_engine.channel_binding import load_channel_binding
    from paper_trading_engine.store import PaperStore

    monkeypatch.setattr(cli, "_validate_strategy", lambda *args: {
        "strategy_id": "S001", "name": "综合基线策略", "version": "v2",
        "release_id": "S001-v2", "release_hash": "b" * 64,
        "qualification": "PAPER_READY", "strategy_payload": {},
    })
    code = cli.main([
        "channel", "bind-strategy", "--repo-root", str(tmp_path), "--channel", "futu",
        "--strategy", "S001", "--strategy-version", "v2", "--actor", "tomxiao",
        "--reason", "人工确认切换",
    ])

    assert code == 0
    document = json.loads(capsys.readouterr().out)
    assert document["result"]["release_id"] == "S001-v2"
    store = PaperStore(tmp_path / "state" / "paper_trading" / "runtime.db")
    try:
        assert load_channel_binding(store).bound_by == "tomxiao"
    finally:
        store.close()


def test_control_restart_uses_runtime_database_without_starting_engine(tmp_path: Path, capsys, monkeypatch) -> None:
    from paper_trading_engine import cli

    calls = []
    monkeypatch.setattr(cli, "_restart_running_pte", lambda args: calls.append(args) or {
        "old_instance_id": "old", "new_instance_id": "new", "status": "READY",
    })

    code = cli.main([
        "control", "restart", "--repo-root", str(tmp_path), "--wait", "12",
    ])

    assert code == 0
    assert calls[0].database == tmp_path.resolve() / "state" / "paper_trading" / "runtime.db"
    assert calls[0].wait == 12
    assert json.loads(capsys.readouterr().out)["command"] == "pte.control.restart"


def test_control_token_is_stable_and_persisted(tmp_path: Path) -> None:
    from paper_trading_engine.cli import _ensure_control_token
    from paper_trading_engine.store import PaperStore

    store = PaperStore(tmp_path / "runtime.db")
    try:
        first = _ensure_control_token(store)
        second = _ensure_control_token(store)
        assert second == first
        assert len(first) >= 32
        assert store.get_setting("control_token") == first
    finally:
        store.close()


def test_restart_waits_for_a_different_healthy_instance(tmp_path: Path, monkeypatch) -> None:
    from paper_trading_engine import cli
    from paper_trading_engine.store import PaperStore

    args = cli.build_parser().parse_args([
        "control", "restart", "--repo-root", str(tmp_path), "--wait", "2",
    ])
    store = PaperStore(args.database)
    store.set_setting("control_token", "secret-token")
    store.close()
    calls = []

    def read_json(url, *, request=None, timeout=3.0):
        calls.append((url, request, timeout))
        if request is not None:
            assert request.get_header("X-pte-control-token") == "secret-token"
            return 202, {"status": "RESTART_ACCEPTED", "instance_id": "old"}
        if len(calls) == 1:
            return 200, {"instance_id": "old"}
        return 200, {"instance_id": "new", "runtime": "RUNNING"}

    monkeypatch.setattr(cli, "_read_json", read_json)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)

    assert cli._restart_running_pte(args) == {
        "status": "READY", "old_instance_id": "old", "new_instance_id": "new",
    }


def test_build_engine_keeps_virtual_accounts_when_futu_initialization_fails(tmp_path: Path, monkeypatch) -> None:
    from paper_trading_engine import cli

    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    monkeypatch.setattr(cli, "seed_runtime_data", lambda *_: None)
    monkeypatch.setattr(cli, "FutuGateway", lambda **_: (_ for _ in ()).throw(RuntimeError("SDK failed")))
    args = cli.build_parser().parse_args(["once", "--repo-root", str(tmp_path)])

    engine = cli.build_engine(args)
    try:
        status = engine.status()
        assert status["channel"]["channel_error"] == "SDK failed"
        assert status["virtual_accounts"][0]["account_id"] == "baseline-143"
        assert status["virtual_accounts"][0]["initial_cash"] == "100000.0000"
        assert status["virtual_accounts"][0]["cash"] == "100000.0000"
    finally:
        engine.close()


def test_build_engine_safely_migrates_pristine_baseline_account_to_default_capital(
    tmp_path: Path, monkeypatch,
) -> None:
    from paper_trading_engine import cli
    from paper_trading_engine.store import PaperStore

    database = tmp_path / "state" / "paper_trading" / "runtime.db"
    store = PaperStore(database)
    store.create_virtual_account(
        "baseline-143", "候选143", "baseline_20260903",
        "a7af8864e469b72a94c59eb2e012af5f9a634203cdf5a0214391dd2909e9e331",
        1_000_000, is_futu_reference=True,
    )
    store.close()
    monkeypatch.setattr(cli, "seed_runtime_data", lambda *_: None)
    monkeypatch.setattr(
        cli, "FutuGateway",
        lambda **_: (_ for _ in ()).throw(RuntimeError("SDK failed")),
    )
    args = cli.build_parser().parse_args(["once", "--repo-root", str(tmp_path)])

    engine = cli.build_engine(args)
    try:
        account = engine.status()["virtual_accounts"][0]
        assert account["initial_cash"] == "100000.0000"
        assert account["cash"] == "100000.0000"
        assert account["total_assets"] == "100000.0000"
    finally:
        engine.close()
