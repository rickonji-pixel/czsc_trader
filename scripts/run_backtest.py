"""Run a validated symbol with one immutable fixed-rule baseline."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys

from czsc_trader.backtest_runner import BacktestRequest, run_fixed_backtest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True, help="Full A-share code, e.g. 600519.SH")
    parser.add_argument("--asset", required=True, choices=("stock", "etf"))
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--baseline")
    parser.add_argument("--targets", type=Path, dest="targets_path")
    parser.add_argument("--fee-rate", type=float, default=0.0005)
    parser.add_argument("--init-cash", type=float, default=1_000_000.0)
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--outputs-root", type=Path, default=Path("outputs"))
    parser.add_argument(
        "--baseline-root", type=Path, default=Path("configs/rule_baselines")
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    request = BacktestRequest(
        symbol=args.symbol,
        asset_type=args.asset,
        start=args.start,
        end=args.end,
        baseline=args.baseline,
        targets_path=args.targets_path,
        fee_rate=args.fee_rate,
        init_cash=args.init_cash,
        raw_dir=args.data_dir,
        outputs_root=args.outputs_root,
        baseline_root=args.baseline_root,
    )
    try:
        summary = run_fixed_backtest(request)
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
