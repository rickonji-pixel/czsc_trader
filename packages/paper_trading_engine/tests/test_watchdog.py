from __future__ import annotations

from collections import deque
from pathlib import Path


class FakeProcess:
    def __init__(self, return_code: int | None = None) -> None:
        self.return_code = return_code
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True
        self.return_code = 0

    def wait(self, timeout: float | None = None) -> int:
        return int(self.return_code or 0)

    def kill(self) -> None:
        self.killed = True
        self.return_code = -9


def test_http_failures_restart_only_after_three_consecutive_failures(tmp_path: Path) -> None:
    from paper_trading_engine.watchdog import Watchdog

    processes = [FakeProcess(), FakeProcess()]
    health = deque([False, False, False])
    delays: list[float] = []
    watchdog = Watchdog(
        command=["pte", "serve"],
        working_directory=tmp_path,
        health_url="http://127.0.0.1:8080/api/status",
        process_factory=lambda *_: processes.pop(0),
        health_check=lambda *_: health.popleft(),
        sleep=delays.append,
    )

    first = watchdog.start_child()
    watchdog.check_once()
    watchdog.check_once()
    assert watchdog.child is first

    watchdog.check_once()
    assert first.terminated is True
    assert watchdog.child is not first
    assert delays == [5.0]


def test_successful_probe_resets_failure_and_restart_backoff(tmp_path: Path) -> None:
    from paper_trading_engine.watchdog import Watchdog

    processes = [FakeProcess(), FakeProcess(), FakeProcess()]
    health = deque([False, False, False, True, False, False, False])
    delays: list[float] = []
    watchdog = Watchdog(
        command=["pte", "serve"], working_directory=tmp_path,
        health_url="http://127.0.0.1:8080/api/status",
        process_factory=lambda *_: processes.pop(0),
        health_check=lambda *_: health.popleft(), sleep=delays.append,
    )

    watchdog.start_child()
    for _ in range(7):
        watchdog.check_once()

    assert delays == [5.0, 5.0]


def test_exited_child_is_restarted_without_waiting_for_http_threshold(tmp_path: Path) -> None:
    from paper_trading_engine.watchdog import Watchdog

    processes = [FakeProcess(return_code=7), FakeProcess()]
    delays: list[float] = []
    watchdog = Watchdog(
        command=["pte", "serve"], working_directory=tmp_path,
        health_url="http://127.0.0.1:8080/api/status",
        process_factory=lambda *_: processes.pop(0),
        health_check=lambda *_: True, sleep=delays.append,
    )

    failed = watchdog.start_child()
    watchdog.check_once()

    assert watchdog.child is not failed
    assert delays == [5.0]


def test_stop_child_terminates_running_process(tmp_path: Path) -> None:
    from paper_trading_engine.watchdog import Watchdog

    process = FakeProcess()
    watchdog = Watchdog(
        command=["pte", "serve"], working_directory=tmp_path,
        health_url="http://127.0.0.1:8080/api/status",
        process_factory=lambda *_: process,
        health_check=lambda *_: True,
    )

    watchdog.start_child()
    watchdog.stop_child()

    assert process.terminated is True
    assert watchdog.child is None


def test_health_probe_requires_pte_status_payload(monkeypatch) -> None:
    from paper_trading_engine import watchdog

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self) -> bytes:
            return b'{"environment":"SIMULATE","symbol":"588080.SH"}'

    monkeypatch.setattr(watchdog, "urlopen", lambda *_args, **_kwargs: Response())
    assert watchdog.http_is_healthy("http://127.0.0.1:8080/api/status", 2.0) is True
