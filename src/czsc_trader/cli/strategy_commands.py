from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable

from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.strategy_service import (
    add_strategy_evidence,
    create_strategy,
    create_strategy_version,
    freeze_strategy_version,
    list_strategies,
    show_strategy,
    strategy_history,
    strategy_performance,
    transition_strategy_version,
    validate_strategies,
)
from czsc_trader.application.evaluation_service import evaluate_experiment


def run_strategy_command(args: argparse.Namespace, context: RepositoryContext):
    action = args.strategy_action
    if action == "list":
        return list_strategies(context)
    if action == "show":
        return show_strategy(context, args.strategy, args.version)
    if action == "history":
        return strategy_history(context, args.strategy)
    if action == "performance":
        return strategy_performance(context, args.strategy, args.version)
    if action == "validate":
        return validate_strategies(context)
    if action == "create":
        return create_strategy(
            context, args.input, actor=args.actor, reason=args.reason
        )
    if action == "version.create":
        return create_strategy_version(
            context, args.input, actor=args.actor, reason=args.reason
        )
    if action == "freeze":
        return freeze_strategy_version(
            context,
            args.strategy,
            args.version,
            args.evidence,
            actor=args.actor,
            reason=args.reason,
        )
    if action in {"promote", "downgrade", "retire"}:
        return transition_strategy_version(
            context,
            action,
            args.strategy,
            args.version,
            actor=args.actor,
            reason=args.reason,
            evidence_ids=getattr(args, "evidence", None),
        )
    if action == "evidence.add":
        return add_strategy_evidence(context, args.input)
    if action == "evaluate":
        return evaluate_experiment(context, args.experiment)
    raise ValueError(f"unknown strategy action: {action}")


def _identity(parser: argparse.ArgumentParser, *, version_optional: bool = False) -> None:
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--version", required=not version_optional)


def _audit(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)


def add_strategy_parser(
    resources: argparse._SubParsersAction,
    add_common: Callable[[argparse.ArgumentParser], None],
    handler: Callable[[argparse.Namespace], object],
) -> None:
    strategy = resources.add_parser("strategy")
    actions = strategy.add_subparsers(
        dest="strategy_action", required=True, parser_class=type(strategy)
    )

    for action in ("list", "validate"):
        leaf = actions.add_parser(action)
        if action == "validate":
            leaf.add_argument("--all", action="store_true")
        add_common(leaf)
        leaf.set_defaults(command_handler=handler, command_name=f"strategy.{action}")

    show = actions.add_parser("show")
    _identity(show, version_optional=True)
    add_common(show)
    show.set_defaults(command_handler=handler, command_name="strategy.show")

    history = actions.add_parser("history")
    history.add_argument("--strategy", required=True)
    add_common(history)
    history.set_defaults(command_handler=handler, command_name="strategy.history")

    performance = actions.add_parser("performance")
    _identity(performance, version_optional=True)
    add_common(performance)
    performance.set_defaults(command_handler=handler, command_name="strategy.performance")

    create = actions.add_parser("create")
    create.add_argument("--input", type=Path, required=True)
    _audit(create)
    add_common(create)
    create.set_defaults(command_handler=handler, command_name="strategy.create")

    version = actions.add_parser("version")
    version_actions = version.add_subparsers(
        dest="version_action", required=True, parser_class=type(strategy)
    )
    version_create = version_actions.add_parser("create")
    version_create.add_argument("--input", type=Path, required=True)
    _audit(version_create)
    add_common(version_create)
    version_create.set_defaults(
        strategy_action="version.create",
        command_handler=handler,
        command_name="strategy.version.create",
    )

    freeze = actions.add_parser("freeze")
    _identity(freeze)
    freeze.add_argument("--evidence", type=Path, required=True)
    _audit(freeze)
    add_common(freeze)
    freeze.set_defaults(command_handler=handler, command_name="strategy.freeze")

    for action in ("promote", "downgrade", "retire"):
        leaf = actions.add_parser(action)
        _identity(leaf)
        if action != "retire":
            leaf.add_argument("--evidence", action="append", required=True)
        _audit(leaf)
        add_common(leaf)
        leaf.set_defaults(command_handler=handler, command_name=f"strategy.{action}")

    evidence = actions.add_parser("evidence")
    evidence_actions = evidence.add_subparsers(
        dest="evidence_action", required=True, parser_class=type(strategy)
    )
    evidence_add = evidence_actions.add_parser("add")
    evidence_add.add_argument("--input", type=Path, required=True)
    add_common(evidence_add)
    evidence_add.set_defaults(
        strategy_action="evidence.add",
        command_handler=handler,
        command_name="strategy.evidence.add",
    )

    evaluate = actions.add_parser("evaluate")
    evaluate.add_argument("--experiment", required=True)
    add_common(evaluate)
    evaluate.set_defaults(command_handler=handler, command_name="strategy.evaluate")
