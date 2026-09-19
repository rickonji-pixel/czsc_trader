"""Standalone command for SRT-owned production data publication."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
from typing import Sequence

from .publisher import StrategyDataPublisher


def _release(value: str) -> tuple[str, str]:
    try:
        strategy_id, version = value.rsplit("-", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("release must look like S007-v1") from exc
    if not strategy_id or not version.startswith("v"):
        raise argparse.ArgumentTypeError("release must look like S007-v1")
    return strategy_id, version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="srt-publish")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--config-root", type=Path)
    parser.add_argument("--data-dir", type=Path, required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    target = actions.add_parser("target")
    target.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    publish = actions.add_parser("publish")
    publish.add_argument("--symbol", required=True)
    publish.add_argument("--asset", choices=("etf", "stock"), required=True)
    publish.add_argument("--release", action="append", type=_release, required=True)
    publish.add_argument("--cutoff", type=date.fromisoformat, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        publisher = StrategyDataPublisher(
            repo_root=args.repo_root,
            config_root=args.config_root,
            data_dir=args.data_dir,
        )
        if args.action == "target":
            result: object = {"data_cutoff": publisher.publication_target(args.as_of)}
        else:
            result = publisher.publish_release(
                args.symbol, args.asset, args.release, args.cutoff.isoformat()
            )
    except Exception as exc:
        print(json.dumps({
            "status": "FAIL",
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "PASS", "result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
