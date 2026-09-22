"""Repository-local temporary workspace governance."""

from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
import time
from uuid import uuid4


_NAMESPACE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def replace_directory(staging: Path, destination: Path) -> None:
    """Atomically publish a directory despite short-lived Windows file locks."""

    attempts = 6 if os.name == "nt" else 1
    for attempt in range(attempts):
        try:
            staging.replace(destination)
            return
        except PermissionError:
            if attempt + 1 == attempts:
                raise
            time.sleep(0.05 * (2**attempt))


def _repository_root(anchor: Path) -> Path | None:
    current = Path(anchor).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "src" / "czsc_trader"
        ).is_dir():
            return candidate
    return None


def temporary_root(anchor: Path, *, repository_root: Path | None = None) -> Path:
    """Return the governed ``.tmp`` root for a repository or isolated caller."""

    if repository_root is not None:
        root = Path(repository_root).resolve()
    else:
        root = _repository_root(Path(anchor)) or Path(anchor).resolve()
    target = root / ".tmp"
    target.mkdir(parents=True, exist_ok=True)
    return target


def create_temporary_directory(
    anchor: Path,
    namespace: str,
    *,
    prefix: str = "run-",
    repository_root: Path | None = None,
) -> Path:
    """Create one unique directory below ``.tmp/<namespace>``."""

    normalized = str(namespace).strip().lower()
    if not _NAMESPACE.fullmatch(normalized):
        raise ValueError("temporary namespace must contain lowercase letters, digits or hyphens")
    if not prefix or Path(prefix).name != prefix or "/" in prefix or "\\" in prefix:
        raise ValueError("temporary prefix must be one path-safe name")
    parent = temporary_root(anchor, repository_root=repository_root) / normalized
    parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # tempfile.mkdtemp uses mode 0700. On Windows that protects the DACL and
        # strips inherited workspace permissions; files atomically moved out of
        # staging retain that private ACL. A normal mkdir preserves inheritance.
        for _ in range(100):
            candidate = parent / f"{prefix}{uuid4().hex}"
            try:
                candidate.mkdir()
            except FileExistsError:
                continue
            return candidate
        raise FileExistsError(f"unable to allocate temporary directory below {parent}")
    return Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
