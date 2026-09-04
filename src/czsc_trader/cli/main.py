from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
import json
from pathlib import Path
import sys
import traceback

from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.errors import CommandError, InternalError, UsageError
from .output import write_error, write_result


@dataclass(frozen=True)
class RawCommandOutput:
    content: str


class CommandParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UsageError("invalid_arguments", message)


def _add_repository_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument("--debug", action="store_true")


def _context(args: argparse.Namespace) -> RepositoryContext:
    context = RepositoryContext.discover(Path.cwd(), explicit_root=args.repo_root)
    data_dir = getattr(args, "data_dir", None)
    if data_dir is not None:
        resolved = data_dir.resolve() if data_dir.is_absolute() else (context.root / data_dir).resolve()
        context = replace(context, raw_dir=resolved)
    return context


def _baseline_list(args: argparse.Namespace):
    from czsc_trader.application.baseline_service import list_baselines

    return list_baselines(_context(args))


def _baseline_show(args: argparse.Namespace):
    from czsc_trader.application.baseline_service import show_baseline

    return show_baseline(_context(args), args.version, symbol=args.symbol)


def _baseline_validate(args: argparse.Namespace):
    from czsc_trader.application.baseline_service import validate_baseline

    return validate_baseline(_context(args), args.version, symbol=args.symbol)


def _data_validate(args: argparse.Namespace):
    from czsc_trader.application.data_service import validate_data

    return validate_data(_context(args), args.symbol)


def _data_prepare(args: argparse.Namespace):
    from czsc_trader.application.data_service import PrepareDataCommand, prepare_data

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
    from czsc_trader.application.backtest_service import BacktestCommand, run_backtest

    return run_backtest(
        _context(args),
        BacktestCommand(
            strategy_id=args.strategy,
            strategy_version=args.strategy_version,
            dataset=args.dataset,
            symbol=args.symbol,
            asset_type=args.asset,
            start=args.start,
            end=args.end,
            init_cash=args.init_cash,
            outputs_root=args.outputs_root,
        ),
    )


def _advice_run(args: argparse.Namespace):
    from czsc_trader.application.advice_service import AdviceCommand, run_advice

    using_cash = args.available_cash is not None
    using_new = args.actual_quantity is not None or args.position_size is not None or using_cash
    using_legacy = args.actual_position is not None or args.quantity is not None
    if using_new and using_legacy:
        raise UsageError("invalid_arguments", "new and legacy quantity arguments cannot be mixed")
    if using_new:
        if args.actual_quantity is None or (args.position_size is None) == (not using_cash):
            raise UsageError(
                "invalid_arguments", "use --actual-quantity with exactly one sizing argument"
            )
        actual_quantity = args.actual_quantity
        position_size = args.position_size
    else:
        if args.actual_position is None or args.quantity is None:
            raise UsageError(
                "invalid_arguments", "provide --actual-quantity/--position-size"
            )
        actual_quantity = args.actual_position * args.quantity
        position_size = args.quantity
    return run_advice(
        _context(args),
        AdviceCommand(
            symbol=args.symbol,
            asset_type=args.asset,
            actual_quantity=actual_quantity,
            position_size=position_size,
            available_cash=args.available_cash,
            baseline=args.baseline,
            strategy=args.strategy,
            strategy_version=args.strategy_version,
            cycle_target_quantity=args.cycle_target_quantity,
        ),
    )


def _repository_path(context: RepositoryContext, value: Path) -> Path:
    return value.resolve() if value.is_absolute() else (context.root / value).resolve()


def _archive_validate(args: argparse.Namespace):
    from czsc_trader.application.archive_service import validate_archives

    context = _context(args)
    archive = _repository_path(context, args.archive) if args.archive else None
    return validate_archives(context, archive, all_archives=args.all)


def _strategy_command(args: argparse.Namespace):
    from czsc_trader.cli.strategy_commands import run_strategy_command

    return run_strategy_command(args, _context(args))


def _chart_observation(args: argparse.Namespace) -> RawCommandOutput:
    from czsc_trader.observation_chart import render_observation_html

    return RawCommandOutput(render_observation_html(json.load(sys.stdin)))


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
    data_prepare.add_argument("--data-dir", type=Path)
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

    from czsc_trader.cli.strategy_commands import add_strategy_parser

    add_strategy_parser(resources, _add_repository_root, _strategy_command)

    chart = resources.add_parser("chart")
    chart_actions = chart.add_subparsers(
        dest="action", required=True, parser_class=CommandParser
    )
    chart_observation = chart_actions.add_parser("observation")
    chart_observation.add_argument("--format", choices=("html",), default="html")
    chart_observation.add_argument("--debug", action="store_true")
    chart_observation.set_defaults(
        command_handler=_chart_observation,
        command_name="chart.observation",
    )

    backtest = resources.add_parser("backtest")
    backtest_actions = backtest.add_subparsers(
        dest="action", required=True, parser_class=CommandParser
    )
    backtest_run = backtest_actions.add_parser("run")
    backtest_run.add_argument("--strategy", required=True)
    backtest_run.add_argument("--strategy-version", required=True)
    backtest_run.add_argument("--dataset", required=True, choices=("research", "backtest"))
    backtest_run.add_argument("--symbol", required=True)
    backtest_run.add_argument("--asset", required=True, choices=("stock", "etf"))
    backtest_run.add_argument("--start", required=True, type=date.fromisoformat)
    backtest_run.add_argument("--end", required=True, type=date.fromisoformat)
    backtest_run.add_argument("--init-cash", required=True, type=float)
    backtest_run.add_argument("--outputs-root", type=Path)
    _add_repository_root(backtest_run)
    backtest_run.set_defaults(
        command_handler=_backtest_run,
        command_name="backtest.run",
    )
    advice = resources.add_parser("advice")
    advice_actions = advice.add_subparsers(
        dest="action", required=True, parser_class=CommandParser
    )
    advice_run = advice_actions.add_parser("run")
    advice_run.add_argument("--symbol", required=True)
    advice_run.add_argument("--asset", required=True, choices=("stock", "etf"))
    advice_run.add_argument("--actual-quantity", type=int)
    advice_run.add_argument("--position-size", type=int)
    advice_run.add_argument("--available-cash", type=float)
    advice_run.add_argument("--actual-position", type=int, choices=(0, 1))
    advice_run.add_argument("--quantity", type=int)
    advice_run.add_argument("--baseline")
    advice_run.add_argument("--strategy")
    advice_run.add_argument("--strategy-version")
    advice_run.add_argument("--cycle-target-quantity", type=int)
    advice_run.add_argument("--data-dir", type=Path)
    _add_repository_root(advice_run)
    advice_run.set_defaults(
        command_handler=_advice_run,
        command_name="advice.run",
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
        if _format_hint(arguments) == "html":
            sys.stderr.write(f"{exc.code}: {exc.message}\n")
            return exc.exit_code
        return write_error(
            _command_hint(arguments),
            exc,
            output_format=_format_hint(arguments),
        )
    command = str(getattr(args, "command_name", args.resource))
    try:
        result = args.command_handler(args)
    except CommandError as exc:
        if args.format == "html":
            sys.stderr.write(f"{exc.code}: {exc.message}\n")
            return exc.exit_code
        return write_error(command, exc, output_format=args.format)
    except Exception as exc:  # pragma: no cover - exercised through --debug later
        if getattr(args, "debug", False):
            traceback.print_exc()
        error = InternalError("internal_error", str(exc))
        if args.format == "html":
            sys.stderr.write(f"{error.code}: {error.message}\n")
            return error.exit_code
        return write_error(
            command,
            error,
            output_format=args.format,
        )
    if isinstance(result, RawCommandOutput):
        sys.stdout.write(result.content)
        return 0
    return write_result(result, output_format=args.format)
