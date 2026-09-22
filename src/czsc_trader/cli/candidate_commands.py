from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable

from czsc_trader.application.context import RepositoryContext


def run_candidate_command(args: argparse.Namespace, context: RepositoryContext):
    from czsc_trader.application.candidate_service import (
        evaluate_candidate,
        freeze_candidate,
        review_candidate,
    )

    if args.candidate_action == "review":
        return review_candidate(context, args.package, args.mandate)
    if args.candidate_action == "evaluate":
        return evaluate_candidate(context, args.candidate_id)
    if args.candidate_action == "freeze":
        return freeze_candidate(context, args.candidate_id, args.change_summary)
    raise ValueError(f"unknown candidate action: {args.candidate_action}")


def add_candidate_parser(
    resources: argparse._SubParsersAction,
    add_common: Callable[[argparse.ArgumentParser], None],
    handler: Callable[[argparse.Namespace], object],
) -> None:
    candidate = resources.add_parser("candidate")
    actions = candidate.add_subparsers(
        dest="candidate_action", required=True, parser_class=type(candidate)
    )

    review = actions.add_parser("review")
    review.add_argument("--package", type=Path, required=True)
    review.add_argument("--mandate", type=Path, required=True)
    add_common(review)
    review.set_defaults(command_handler=handler, command_name="candidate.review")

    evaluate = actions.add_parser("evaluate")
    evaluate.add_argument("candidate_id")
    add_common(evaluate)
    evaluate.set_defaults(command_handler=handler, command_name="candidate.evaluate")

    freeze = actions.add_parser("freeze")
    freeze.add_argument("candidate_id")
    freeze.add_argument("--change-summary", required=True)
    add_common(freeze)
    freeze.set_defaults(command_handler=handler, command_name="candidate.freeze")
