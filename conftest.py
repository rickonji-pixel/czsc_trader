"""Repository-wide pytest temporary-directory bootstrap."""

from collections.abc import Iterator
import os
from pathlib import Path
import shutil
from uuid import uuid4

import pytest


_PYTEST_RUN = (
    Path(__file__).resolve().parent / ".tmp" / "pytest" / f"run-{uuid4().hex}"
)


def _create_directory(path: Path) -> None:
    """Create a private directory while retaining inherited ACLs on Windows."""

    mode = 0o777 if os.name == "nt" else 0o700
    path.mkdir(mode=mode, parents=True, exist_ok=False)


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """Return an isolated repository-local path without pytest's Windows ACL rewrite."""

    _PYTEST_RUN.parent.mkdir(parents=True, exist_ok=True)
    _PYTEST_RUN.mkdir(exist_ok=True)
    path = _PYTEST_RUN / f"case-{uuid4().hex}"
    _create_directory(path)
    yield path


def pytest_sessionfinish() -> None:
    """Remove the current process workspace when pytest finishes."""

    shutil.rmtree(_PYTEST_RUN, ignore_errors=True)
