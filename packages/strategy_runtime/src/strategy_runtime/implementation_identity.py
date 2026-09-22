"""Stable source-closure identities for frozen strategy implementations."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
from pathlib import Path, PurePosixPath
from collections.abc import Iterator

from .errors import RuntimeCompatibilityError


_SOURCE_ROOT: ContextVar[Path | None] = ContextVar("srt_source_root", default=None)


@contextmanager
def strategy_source_root(source_root: Path | None) -> Iterator[None]:
    """Bind implicit implementation hashes to the release package being loaded."""

    token = _SOURCE_ROOT.set(None if source_root is None else Path(source_root).resolve())
    try:
        yield
    finally:
        _SOURCE_ROOT.reset(token)


def implementation_sha256(
    source_files: tuple[str, ...], *, source_root: Path | None = None,
) -> str:
    root = Path(source_root).resolve() if source_root is not None else _SOURCE_ROOT.get()
    if root is None:
        raise RuntimeCompatibilityError("strategy source root is required")
    digest = sha256()
    for name in sorted(source_files):
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeCompatibilityError("implementation source path is unsafe")
        resource = root.joinpath(*relative.parts)
        if not resource.is_file():
            raise RuntimeCompatibilityError(f"implementation source is missing: {name}")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(resource.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
