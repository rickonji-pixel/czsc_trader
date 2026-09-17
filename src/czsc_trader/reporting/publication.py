from __future__ import annotations

from datetime import date
from itertools import count
import os
from pathlib import Path
import time


def _replace_directory(staging: Path, destination: Path) -> None:
    """Rename a completed run, tolerating short-lived Windows file locks."""

    attempts = 6 if os.name == "nt" else 1
    for attempt in range(attempts):
        try:
            staging.replace(destination)
            return
        except PermissionError:
            if attempt + 1 == attempts:
                raise
            time.sleep(0.05 * (2**attempt))


def publish_run_directory(
    staging: Path,
    outputs_root: Path,
    symbol: str,
    run_date: date,
) -> Path:
    """Atomically publish a complete run without overwriting prior revisions."""
    staging = Path(staging)
    outputs_root = Path(outputs_root)
    outputs_root.mkdir(parents=True, exist_ok=True)
    code = symbol.split(".", maxsplit=1)[0]
    for revision in count(1):
        destination = outputs_root / f"{code}_{run_date:%m%d}_BT{revision:02d}"
        if destination.exists():
            continue
        try:
            _replace_directory(staging, destination)
        except FileExistsError:
            continue
        return destination.resolve()
