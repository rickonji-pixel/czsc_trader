"""Evaluate one tracked frozen challenger on the 2026 holdout."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from czsc_trader.experiments import run_2026_holdout
from czsc_trader.research import create_output_dir


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen-challenger", required=True, type=Path)
    return parser


if __name__ == "__main__":
    args = _parser().parse_args()
    output_dir = create_output_dir(
        Path("outputs"),
        "588080.SH",
        datetime.now(ZoneInfo("Asia/Shanghai")).date(),
    )
    summary = run_2026_holdout(
        Path("data/raw"),
        Path("configs/rule_baselines"),
        args.frozen_challenger,
        output_dir,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
