"""Reentrant, non-blocking ownership of one registry's write transactions."""

from contextlib import contextmanager
from functools import wraps
import os
from pathlib import Path
import sys
from threading import RLock

from .errors import RegistryError


class RegistryWriteLock:
    def __init__(self, root: Path):
        self.path = root / ".registry.lock"
        self._thread_lock = RLock()
        self._depth = 0

    @contextmanager
    def hold(self):
        with self._thread_lock:
            if self._depth:
                yield
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a+b") as handle:
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
                    raise RegistryError(f"cannot acquire registry write lock: {self.path}") from exc
                self._depth = 1
                try:
                    yield
                finally:
                    self._depth = 0
                    handle.seek(0)
                    if sys.platform == "win32":
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            # Keep the lock file: unlinking it can let another writer lock a
            # different inode while a competing process still holds the old one.


def registry_write(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._write_lock.hold():
            return method(self, *args, **kwargs)
    return guarded
