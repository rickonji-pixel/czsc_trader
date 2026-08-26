"""Non-overwriting local output paths for ordinary fixed-rule backtests."""

from __future__ import annotations

from datetime import date
from itertools import count
from pathlib import Path


def create_output_dir(outputs_root: Path, symbol: str, run_date: date) -> Path:
    """Atomically create the next revision directory for a dated symbol run."""
    symbol_code = symbol.split(".", maxsplit=1)[0]
    outputs_root = Path(outputs_root)
    outputs_root.mkdir(parents=True, exist_ok=True)
    for revision in count(1):
        output_dir = outputs_root / f"{symbol_code}_{run_date:%m%d}_BT{revision:02d}"
        try:
            output_dir.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return output_dir
