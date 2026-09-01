from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import date
from pathlib import Path
import sys
import traceback

from czsc_trader.application.baseline_service import (
    list_baselines,
    show_baseline,
    validate_baseline,
)
from czsc_trader.application.backtest_service import BacktestCommand, run_backtest
from czsc_trader.application.archive_service import validate_archives
from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.data_service import (
    PrepareDataCommand,
    prepare_data,
    validate_data,
)
from czsc_trader.application.errors import CommandError, InternalError, UsageError
from .output import write_error, write_result


class CommandParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UsageError("invalid_arguments", message)


def _add_repository_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument("--debug", action="store_true")


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


def _data_prepare(args: argparse.Namespace):
    return prepare_data(
        _context(args),
        PrepareDataCommand(
            symbol=args.symbol,
            asset_type=args.asset,
            start=args.start,
            end=args.end,
        ),
    )


def _backtest_run(args: argparse.Namespace):
    return run_backtest(
        _context(args),
        BacktestCommand(
            symbol=args.symbol,
            asset_type=args.asset,
            start=args.start,
            end=args.end,
            baseline=args.baseline,
            windows_path=args.windows,
            window=args.window,
            fee_rate=args.fee_rate,
            init_cash=args.init_cash,
            outputs_root=args.outputs_root,
        ),
    )


def _repository_path(context: RepositoryContext, value: Path) -> Path:
    return value.resolve() if value.is_absolute() else (context.root / value).resolve()


def _archive_validate(args: argparse.Namespace):
    context = _context(args)
    archive = _repository_path(context, args.archive) if args.archive else None
    return validate_archives(context, archive, all_archives=args.all)


def build_parser() -> argparse.ArgumentParser:
    parser = CommandParser(prog="czsc-trader")
    resources = parser.add_subparsers(
        dest="resource", required=True, parser_class=CommandParser
    )

    data = resources.add_parser("data")
    data_actions = data.add_subparsers(
        dest="action", required=True, parser_class=CommandParser
    )
    data_prepare = data_actions.add_parser("prepare")
    data_prepare.add_argument("--symbol", required=True)
    data_prepare.add_argument("--asset", required=True, choices=("stock", "etf"))
    data_prepare.add_argument("--start", required=True, type=date.fromisoformat)
    data_prepare.add_argument("--end", required=True, type=date.fromisoformat)
    _add_repository_root(data_prepare)
    data_prepare.set_defaults(
        command_handler=_data_prepare,
        command_name="data.prepare",
    )
    data_validate = data_actions.add_parser("validate")
    data_validate.add_argument("--symbol", required=True)
    _add_repository_root(data_validate)
    data_validate.set_defaults(command_handler=_data_validate, command_name="data.validate")

    baseline = resources.add_parser("baseline")
    baseline_actions = baseline.add_subparsers(
        dest="action", required=True, parser_class=CommandParser
    )
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
    backtest_actions = backtest.add_subparsers(
        dest="action", required=True, parser_class=CommandParser
    )
    backtest_run = backtest_actions.add_parser("run")
    backtest_run.add_argument("--symbol", required=True)
    backtest_run.add_argument("--asset", required=True, choices=("stock", "etf"))
    backtest_run.add_argument("--start", type=date.fromisoformat)
    backtest_run.add_argument("--end", type=date.fromisoformat)
    backtest_run.add_argument("--baseline")
    backtest_run.add_argument("--windows", type=Path)
    backtest_run.add_argument("--window")
    backtest_run.add_argument("--fee-rate", type=float, default=0.0005)
    backtest_run.add_argument("--init-cash", type=float, default=1_000_000.0)
    backtest_run.add_argument("--outputs-root", type=Path)
    _add_repository_root(backtest_run)
    backtest_run.set_defaults(
        command_handler=_backtest_run,
        command_name="backtest.run",
    )
    archive = resources.add_parser("archive")
    archive_actions = archive.add_subparsers(
        dest="action", required=True, parser_class=CommandParser
    )
    archive_validate = archive_actions.add_parser("validate")
    selection = archive_validate.add_mutually_exclusive_group(required=True)
    selection.add_argument("--archive", type=Path)
    selection.add_argument("--all", action="store_true")
    _add_repository_root(archive_validate)
    archive_validate.set_defaults(
        command_handler=_archive_validate,
        command_name="archive.validate",
    )
    return parser


def _command_hint(arguments: Sequence[str]) -> str:
    positional = [value for value in arguments[:2] if not value.startswith("-")]
    return ".".join(positional) if positional else "czsc-trader"


def _format_hint(arguments: Sequence[str]) -> str:
    for index, value in enumerate(arguments):
        if value == "--format" and index + 1 < len(arguments):
            return str(arguments[index + 1])
        if value.startswith("--format="):
            return value.partition("=")[2]
    return "json"


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser.parse_args(arguments)
    except UsageError as exc:
        return write_error(
            _command_hint(arguments),
            exc,
            output_format=_format_hint(arguments),
        )
    command = str(getattr(args, "command_name", args.resource))
    try:
        result = args.command_handler(args)
    except CommandError as exc:
        return write_error(command, exc, output_format=args.format)
    except Exception as exc:  # pragma: no cover - exercised through --debug later
        if getattr(args, "debug", False):
            traceback.print_exc()
        return write_error(
            command,
            InternalError("internal_error", str(exc)),
            output_format=args.format,
        )
    return write_result(result, output_format=args.format)
