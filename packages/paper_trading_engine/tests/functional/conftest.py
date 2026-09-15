from collections.abc import Iterator
import os
from pathlib import Path
import shutil
import sys
from uuid import uuid4

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
_PYTEST_RUN = REPOSITORY_ROOT / ".tmp" / "pytest" / f"pte-run-{uuid4().hex}"
sys.path.insert(0, str(PACKAGE_ROOT / "src"))


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """Keep PTE tests inside the repository and avoid pytest's Windows ACL rewrite."""
    _PYTEST_RUN.parent.mkdir(parents=True, exist_ok=True)
    _PYTEST_RUN.mkdir(exist_ok=True)
    path = _PYTEST_RUN / f"case-{uuid4().hex}"
    mode = 0o777 if os.name == "nt" else 0o700
    path.mkdir(mode=mode, parents=True, exist_ok=False)
    yield path


def pytest_sessionfinish() -> None:
    shutil.rmtree(_PYTEST_RUN, ignore_errors=True)
