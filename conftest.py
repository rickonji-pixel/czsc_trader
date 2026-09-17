"""Repository-wide pytest temporary-directory bootstrap."""

from collections.abc import Iterator
import os
from pathlib import Path
import shutil
import sys
from uuid import uuid4

import pytest


_REPOSITORY_ROOT = Path(__file__).resolve().parent
_EXPECTED_VENV = (_REPOSITORY_ROOT / ".venv").resolve()
_PYTEST_RUN = _REPOSITORY_ROOT / ".tmp" / "pytest" / f"run-{uuid4().hex}"
_PYTHON_CACHE_ROOT = _REPOSITORY_ROOT / ".tmp" / "pycache"

if Path(sys.prefix).resolve() != _EXPECTED_VENV:
    raise pytest.UsageError(
        "CZSC Trader tests must use the repository virtual environment. "
        f"Current interpreter: {sys.executable}. "
        r"Run .\.venv\Scripts\python.exe -m pytest -c pyproject.toml ..."
    )

# The root conftest itself is compiled before this assignment. All modules loaded
# afterwards are redirected, and pytest_sessionfinish removes that bootstrap cache.
sys.pycache_prefix = str(_PYTHON_CACHE_ROOT)


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
    """Remove the process workspace and pytest's root bootstrap bytecode."""

    shutil.rmtree(_PYTEST_RUN, ignore_errors=True)
    shutil.rmtree(_REPOSITORY_ROOT / "__pycache__", ignore_errors=True)
