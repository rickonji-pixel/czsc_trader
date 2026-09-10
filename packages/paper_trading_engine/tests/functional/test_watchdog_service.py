from collections import deque
import json
from pathlib import Path

import pytest

from paper_trading_engine.audit import AuditRecorder
from paper_trading_engine.cli import PortUnavailableError, _record_service_lifecycle, probe_port
from paper_trading_engine.service_config import ServiceConfig
from paper_trading_engine.store import PaperStore
from paper_trading_engine.watchdog import Watchdog
from paper_trading_engine.windows_service import service_commands


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
    assert config.health_url == "http://127.0.0.1:8080/api/system/status"
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
