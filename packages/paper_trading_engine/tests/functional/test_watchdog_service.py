from collections import deque
import json
from pathlib import Path

import pytest

from paper_trading_engine.audit import AuditRecorder
from paper_trading_engine.cli import PortUnavailableError, _record_service_lifecycle, probe_port
from paper_trading_engine.service_config import ServiceConfig
from paper_trading_engine.store import PaperStore, backup_runtime_database
from paper_trading_engine.runtime_lock import RuntimeAlreadyOwnedError, RuntimeDatabaseLock
from paper_trading_engine.watchdog import Watchdog, rotate_log
from paper_trading_engine.windows_service import service_commands
from paper_trading_engine.web_api import PteWebApi


class Process:
    def __init__(self, code=None): self.code, self.terminated = code, False
    def poll(self): return self.code
    def terminate(self): self.terminated, self.code = True, 0
    def wait(self, timeout=None): return self.code or 0
    def kill(self): self.code = -9


def test_ft_pte06_watchdog_service_config_port_and_recovery(tmp_path):
    audit_store = PaperStore(tmp_path / "lifecycle.db")
    _record_service_lifecycle(
        AuditRecorder(audit_store), "SERVICE_STARTED", "instance-1", port=8080,
    )
    lifecycle = audit_store.recent_events(1)[0]
    assert lifecycle["event_type"] == "SERVICE_STARTED"
    assert lifecycle["actor_type"] == "ENGINE"
    assert lifecycle["actor_id"] == "instance-1"
    audit_store.close()
    backup = backup_runtime_database(tmp_path / "lifecycle.db", retention=2)
    assert backup is not None and backup.is_file()
    reopened = PaperStore(backup)
    assert reopened.recent_events(1)[0]["event_type"] == "SERVICE_STARTED"
    reopened.close()

    child_log = tmp_path / "pte.log"
    child_log.write_bytes(b"x" * 32)
    rotate_log(child_log, max_bytes=16, backups=2)
    assert not child_log.exists()
    assert (tmp_path / "pte.log.1").read_bytes() == b"x" * 32

    owner = RuntimeDatabaseLock(tmp_path / "runtime.db").acquire()
    try:
        with pytest.raises(RuntimeAlreadyOwnedError, match="already owned"):
            RuntimeDatabaseLock(tmp_path / "runtime.db").acquire()
    finally:
        owner.release()
    second_owner = RuntimeDatabaseLock(tmp_path / "runtime.db").acquire()
    second_owner.release()

    config = ServiceConfig(repo_root=tmp_path.resolve())
    path = tmp_path / "service.json"
    config.save(path)
    assert ServiceConfig.load(path).serve_arguments() == [
        "serve", "--repo-root", str(tmp_path.resolve()),
        "--host", "127.0.0.1", "--port", "8080",
    ]
    assert set(json.loads(path.read_text()).keys()) == {
        "repo_root", "host", "port",
    }
    assert config.health_url == "http://127.0.0.1:8080/api/health"
    with pytest.raises(ValueError, match="localhost"):
        ServiceConfig(repo_root=tmp_path.resolve(), host="0.0.0.0")

    commands = service_commands(Path("C:/Python/pythonservice.exe"), Path("D:/repo/windows_service.py"))
    assert commands[0][-2:] == ["--startup", "auto"]
    assert commands[1][0:3] == ["sc.exe", "failure", "CZSC-PTE-Watchdog"]

    processes, health, delays = [Process(), Process()], deque([False, False, False]), []
    watchdog = Watchdog(command=["pte", "serve"], working_directory=tmp_path,
                        health_url=config.health_url,
                        process_factory=lambda *_: processes.pop(0),
                        health_check=lambda *_: health.popleft(), sleep=delays.append)
    old = watchdog.start_child()
    watchdog.check_once()
    watchdog.check_once()
    watchdog.check_once()
    assert old.terminated and watchdog.child is not old and delays == [5.0]

    import socket
    occupied = socket.socket()
    occupied.bind(("127.0.0.1", 0))
    try:
        with pytest.raises(PortUnavailableError):
            probe_port("127.0.0.1", occupied.getsockname()[1])
    finally:
        occupied.close()


def test_ft_pte06_business_health_exposes_stalled_scheduler(tmp_path):
    store = PaperStore(tmp_path / "health.db")
    store.set_setting("scheduler_heartbeat_at", "2026-09-01T00:00:00+00:00")

    class Channel:
        @staticmethod
        def status():
            return {
                "alerts": [], "scheduler_failures": [],
                "reconciliation_status": "OK", "account": None,
            }

    class Operations:
        def __init__(self):
            self.store = store
            self.channel = Channel()
            self.virtual = object()

    status = PteWebApi(Operations()).system_status()
    assert status["runtime"] == "RUNNING"
    assert status["watchdog_healthy"] is False
    assert "SCHEDULER_STALLED" in status["alerts"]
    assert PteWebApi(Operations()).health() == {
        "runtime": "RUNNING",
        "watchdog_healthy": False,
        "scheduler_heartbeat_at": "2026-09-01T00:00:00+00:00",
    }
    store.close()


def test_business_health_treats_missing_heartbeat_and_channel_alert_as_degraded(tmp_path):
    store = PaperStore(tmp_path / "missing-heartbeat.db")

    class Channel:
        @staticmethod
        def status():
            return {
                "alerts": ["CHANNEL_CASH_MISMATCH"], "scheduler_failures": [],
                "reconciliation_status": "OK", "account": None,
            }

    class Operations:
        def __init__(self):
            self.store = store
            self.channel = Channel()
            self.virtual = object()

    status = PteWebApi(Operations()).system_status()
    assert status["watchdog_healthy"] is False
    assert status["futu_connection"] == "DEGRADED"
    assert "SCHEDULER_STALLED" in status["alerts"]
    store.close()
