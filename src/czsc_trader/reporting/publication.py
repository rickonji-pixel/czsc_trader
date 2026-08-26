from __future__ import annotations

from datetime import date
from itertools import count
from pathlib import Path


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
            staging.replace(destination)
        except FileExistsError:
            continue
        return destination.resolve()
