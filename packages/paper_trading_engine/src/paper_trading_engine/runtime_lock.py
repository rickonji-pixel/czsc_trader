"""Cross-process ownership lock for the writable PTE runtime database."""

from __future__ import annotations

from pathlib import Path
import os
import sys
from typing import BinaryIO


class RuntimeAlreadyOwnedError(RuntimeError):
    """Another PTE process already owns the writable runtime database."""


class RuntimeDatabaseLock:
    """Hold one non-blocking OS lock for the lifetime of a PTE writer process."""

    def __init__(self, database: Path) -> None:
        database = Path(database)
        self.path = database.with_name(f"{database.name}.lock")
        self._file: BinaryIO | None = None

    def acquire(self) -> RuntimeDatabaseLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeAlreadyOwnedError(
                f"PTE runtime database is already owned by another process: {self.path}"
            ) from exc
        self._file = handle
        return self

    def release(self) -> None:
        handle, self._file = self._file, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> RuntimeDatabaseLock:
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
