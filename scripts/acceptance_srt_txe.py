"""Offline economic parity check for the five frozen SRT implementations.

Capture before a migration, then compare after it. Never publishes market data,
modifies a strategy, or connects to PTE. Evidence belongs in .tmp.
"""

from __future__ import annotations

import argparse
from datetime import date
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.backtesting.srt_bridge import (
    build_srt_signal_replay,
    execution_intraday_frequencies,
    load_srt_strategy,
    replay_srt_account,
)
from czsc_trader.backtesting.strategy_source import resolve_registered_strategy


def capture(root: Path) -> dict:
    context = RepositoryContext.discover(root)
    evidence = {}
    for reference, symbol in (
        ("S001-v1", "588080.SH"),
        ("S001-v2", "588080.SH"),
        ("S002-v1", "510500.SH"),
        ("S003-v1", "510500.SH"),
        ("S007-v1", "588080.SH"),
    ):
        family, version = reference.split("-")
        snapshot = resolve_registered_strategy(context, family, version)
        release, runtime = load_srt_strategy(root, reference)
        frequencies = execution_intraday_frequencies(runtime)
        data = load_replay_data(
            context,
            "backtest",
            symbol,
            "etf",
            date(2026, 9, 15),
            include_five_minute="5m" in frequencies,
            include_one_minute="1m" in frequencies,
        )
        strategy, signals = build_srt_signal_replay(
            snapshot=snapshot,
            replay_data=data,
            start=pd.Timestamp("2026-01-05"),
            end=pd.Timestamp("2026-09-15"),
            repository_root=root,
        )
        result = replay_srt_account(
            strategy=strategy,
            signals=signals,
            replay_data=data,
            initial_cash=100_000,
        )
        tables = {}
        for name in ("decisions", "orders", "fills", "account_daily", "trades"):
            frame = getattr(result, name)
            tables[name] = {
                "rows": len(frame),
                "sha256": sha256(
                    frame.to_csv(index=False, lineterminator="\n").encode()
                ).hexdigest(),
            }
        evidence[reference] = {
            "release_hash": release.release_hash,
            "runtime_hash": strategy.definition.runtime_sha256,
            "dataset_hash": data.fingerprint,
            "tables": tables,
            "metrics": calculate_metrics(result, 100_000),
        }
        print(f"{reference}: {len(result.orders)} orders, {len(result.trades)} trades", flush=True)
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if not output.is_relative_to(root / ".tmp"):
        parser.error("acceptance output must stay inside the repository .tmp directory")
    result = capture(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    if args.compare:
        expected = json.loads(args.compare.read_text(encoding="utf-8"))
        mismatches = [key for key in result if result[key] != expected.get(key)]
        if mismatches or set(expected) != set(result):
            raise RuntimeError(f"economic replay parity failed: {mismatches}")
        print("PASS: five releases, all ledger tables and metrics exactly match", flush=True)


if __name__ == "__main__":
    main()
