from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable

from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.strategy_service import (
    add_strategy_evidence,
    list_strategies,
    show_strategy,
    show_strategy_deployments,
    strategy_history,
    strategy_performance,
    transition_strategy_version,
    validate_strategies,
)


def run_strategy_command(args: argparse.Namespace, context: RepositoryContext):
    action = args.strategy_action
    if action == "list":
        return list_strategies(context)
    if action == "show":
        return show_strategy(context, args.strategy, args.version)
    if action == "deployments":
        return show_strategy_deployments(context, args.release)
    if action == "history":
        return strategy_history(context, args.strategy)
    if action == "performance":
        return strategy_performance(context, args.strategy, args.version)
    if action == "validate":
        return validate_strategies(context)
    if action == "review.open":
        from czsc_trader.application.freeze_review_service import open_freeze_review

        return open_freeze_review(
            context,
            credential_id=args.credential,
            candidate_path=args.candidate,
            mandate_path=args.mandate,
            actor=args.actor,
            reason=args.reason,
        )
    if action == "review.evaluate":
        from czsc_trader.application.freeze_review_service import evaluate_freeze_review

        return evaluate_freeze_review(context, args.strategy, args.credential)
    if action == "review.show":
        from czsc_trader.application.freeze_review_service import show_freeze_review

        return show_freeze_review(context, args.strategy, args.credential)
    if action == "freeze":
        from czsc_trader.application.freeze_review_service import freeze_review_candidate

        return freeze_review_candidate(
            context,
            args.strategy,
            args.credential,
            actor=args.actor,
            reason=args.reason,
            change_summary=args.change_summary,
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

    deployments = actions.add_parser("deployments")
    deployments.add_argument("--release", action="append", required=True)
    add_common(deployments)
    deployments.set_defaults(
        command_handler=handler, command_name="strategy.deployments",
    )

    history = actions.add_parser("history")
    history.add_argument("--strategy", required=True)
    add_common(history)
    history.set_defaults(command_handler=handler, command_name="strategy.history")

    performance = actions.add_parser("performance")
    _identity(performance, version_optional=True)
    add_common(performance)
    performance.set_defaults(command_handler=handler, command_name="strategy.performance")

    review = actions.add_parser("review")
    review_actions = review.add_subparsers(
        dest="review_action", required=True, parser_class=type(strategy)
    )
    review_open = review_actions.add_parser("open")
    review_open.add_argument("--credential", required=True)
    review_open.add_argument("--candidate", type=Path, required=True)
    review_open.add_argument("--mandate", type=Path, required=True)
    _audit(review_open)
    add_common(review_open)
    review_open.set_defaults(
        strategy_action="review.open",
        command_handler=handler,
        command_name="strategy.review.open",
    )
    for action in ("evaluate", "show"):
        leaf = review_actions.add_parser(action)
        leaf.add_argument("--strategy", required=True)
        leaf.add_argument("--credential", required=True)
        add_common(leaf)
        leaf.set_defaults(
            strategy_action=f"review.{action}",
            command_handler=handler,
            command_name=f"strategy.review.{action}",
        )

    freeze = actions.add_parser("freeze")
    freeze.add_argument("--strategy", required=True)
    freeze.add_argument("--credential", required=True)
    freeze.add_argument("--change-summary", required=True)
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
