from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable

from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.research_governance_service import (
    create_research_batch,
    update_research_intent,
)


def run_research_command(args: argparse.Namespace, context: RepositoryContext):
    if args.research_action == "create":
        return create_research_batch(
            context, args.input, actor=args.actor, reason=args.reason
        )
    if args.research_action == "intent.update":
        return update_research_intent(
            context,
            args.strategy,
            args.input,
            actor=args.actor,
            reason=args.reason,
        )
    if args.research_action == "evaluate":
        from czsc_trader.application.research_evaluation_service import (
            evaluate_research_experiment,
        )

        return evaluate_research_experiment(context, args.experiment_id)
    raise ValueError(f"unknown research action: {args.research_action}")


def _audit(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)


def add_research_parser(
    resources: argparse._SubParsersAction,
    add_common: Callable[[argparse.ArgumentParser], None],
    handler: Callable[[argparse.Namespace], object],
) -> None:
    research = resources.add_parser("research")
    actions = research.add_subparsers(
        dest="research_action", required=True, parser_class=type(research)
    )
    create = actions.add_parser("create")
    create.add_argument("--input", type=Path, required=True)
    _audit(create)
    add_common(create)
    create.set_defaults(command_handler=handler, command_name="research.create")

    evaluate = actions.add_parser("evaluate")
    evaluate.add_argument("experiment_id")
    add_common(evaluate)
    evaluate.set_defaults(command_handler=handler, command_name="research.evaluate")

    intent = actions.add_parser("intent")
    intent_actions = intent.add_subparsers(
        dest="intent_action", required=True, parser_class=type(research)
    )
    update = intent_actions.add_parser("update")
    update.add_argument("--strategy", required=True)
    update.add_argument("--input", type=Path, required=True)
    _audit(update)
    add_common(update)
    update.set_defaults(
        research_action="intent.update",
        command_handler=handler,
        command_name="research.intent.update",
    )
