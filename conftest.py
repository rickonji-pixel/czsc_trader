"""Repository-wide pytest temporary-directory bootstrap."""

from pathlib import Path
import shutil
from uuid import uuid4


_PYTEST_RUN = (
    Path(__file__).resolve().parent / ".tmp" / "pytest" / f"run-{uuid4().hex}"
)


def pytest_configure(config) -> None:
    """Give every test process a fresh repository-local base directory."""

    _PYTEST_RUN.parent.mkdir(parents=True, exist_ok=True)
    config.option.basetemp = str(_PYTEST_RUN)


def pytest_sessionfinish() -> None:
    """Remove the current process workspace when pytest finishes."""

    shutil.rmtree(_PYTEST_RUN, ignore_errors=True)
