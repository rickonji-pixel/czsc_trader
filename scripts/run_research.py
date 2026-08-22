"""Run the complete local-data research pipeline."""

from __future__ import annotations

import json
from pathlib import Path

from czsc_trader.research import run_dated_research


if __name__ == "__main__":
    summary = run_dated_research(Path("data/raw"), Path("outputs"))
    print(json.dumps(summary, ensure_ascii=False, indent=2))

