"""Prepare validated flat A-share stock or ETF data."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path

from czsc_trader.market_data_prep import prepare_market_data


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True, help="Full Tushare code, e.g. 600519.SH")
    parser.add_argument("--asset", required=True, choices=("stock", "etf"))
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    return parser


if __name__ == "__main__":
    args = _parser().parse_args()
    result = prepare_market_data(args.symbol, args.asset, args.start, args.end, args.data_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
