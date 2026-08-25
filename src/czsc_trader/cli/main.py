from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import date
from pathlib import Path
import traceback

from czsc_trader.application.baseline_service import (
    list_baselines,
    show_baseline,
    validate_baseline,
)
from czsc_trader.application.backtest_service import BacktestCommand, run_backtest
from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.data_service import validate_data
from czsc_trader.application.errors import CommandError, InternalError

from .output import write_error, write_result


def _add_repository_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", type=Path)


def _context(args: argparse.Namespace) -> RepositoryContext:
    return RepositoryContext.discover(Path.cwd(), explicit_root=args.repo_root)


def _baseline_list(args: argparse.Namespace):
    return list_baselines(_context(args))


def _baseline_show(args: argparse.Namespace):
    return show_baseline(_context(args), args.version, symbol=args.symbol)


def _baseline_validate(args: argparse.Namespace):
    return validate_baseline(_context(args), args.version, symbol=args.symbol)


def _data_validate(args: argparse.Namespace):
    return validate_data(_context(args), args.symbol)


def _backtest_run(args: argparse.Namespace):
    return run_backtest(
        _context(args),
        BacktestCommand(
            symbol=args.symbol,
            asset_type=args.asset,
            start=args.start,
            end=args.end,
            baseline=args.baseline,
            targets_path=args.targets,
            fee_rate=args.fee_rate,
            init_cash=args.init_cash,
            outputs_root=args.outputs_root,
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="czsc-trader")
    resources = parser.add_subparsers(dest="resource", required=True)

    data = resources.add_parser("data")
    data_actions = data.add_subparsers(dest="action", required=True)
    data_validate = data_actions.add_parser("validate")
    data_validate.add_argument("--symbol", required=True)
    _add_repository_root(data_validate)
    data_validate.set_defaults(command_handler=_data_validate, command_name="data.validate")

    baseline = resources.add_parser("baseline")
    baseline_actions = baseline.add_subparsers(dest="action", required=True)
    baseline_list = baseline_actions.add_parser("list")
    _add_repository_root(baseline_list)
    baseline_list.set_defaults(
        command_handler=_baseline_list, command_name="baseline.list"
    )
    for action, handler in (("show", _baseline_show), ("validate", _baseline_validate)):
        leaf = baseline_actions.add_parser(action)
        leaf.add_argument("--version", required=True)
        leaf.add_argument("--symbol")
        _add_repository_root(leaf)
        leaf.set_defaults(
            command_handler=handler,
            command_name=f"baseline.{action}",
        )

    backtest = resources.add_parser("backtest")
    backtest_actions = backtest.add_subparsers(dest="action", required=True)
    backtest_run = backtest_actions.add_parser("run")
    backtest_run.add_argument("--symbol", required=True)
    backtest_run.add_argument("--asset", required=True, choices=("stock", "etf"))
    backtest_run.add_argument("--start", type=date.fromisoformat)
    backtest_run.add_argument("--end", type=date.fromisoformat)
    backtest_run.add_argument("--baseline")
    backtest_run.add_argument("--targets", type=Path)
    backtest_run.add_argument("--fee-rate", type=float, default=0.0005)
    backtest_run.add_argument("--init-cash", type=float, default=1_000_000.0)
    backtest_run.add_argument("--outputs-root", type=Path)
    _add_repository_root(backtest_run)
    backtest_run.set_defaults(
        command_handler=_backtest_run,
        command_name="backtest.run",
    )
    resources.add_parser("experiment")
    resources.add_parser("archive")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = str(getattr(args, "command_name", args.resource))
    try:
        result = args.command_handler(args)
    except CommandError as exc:
        return write_error(command, exc)
    except Exception as exc:  # pragma: no cover - exercised through --debug later
        if getattr(args, "debug", False):
            traceback.print_exc()
        return write_error(
            command,
            InternalError("internal_error", str(exc)),
        )
    return write_result(result)
