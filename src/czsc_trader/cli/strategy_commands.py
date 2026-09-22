from __future__ import annotations

import argparse
from typing import Callable

from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.strategy_runtime_service import (
    deploy_strategy,
    list_installed_strategies,
    strategy_info,
)
from czsc_trader.application.strategy_service import show_strategy_deployments


def run_strategy_command(args: argparse.Namespace, context: RepositoryContext):
    action = args.strategy_action
    if action == "list":
        return list_installed_strategies(context, args.strategy_id)
    if action == "info":
        return strategy_info(context, args.strategy_version_id)
    if action == "deploy":
        return deploy_strategy(context, args.strategy_version_id)
    if action == "deployments":
        return show_strategy_deployments(context, args.release)
    raise ValueError(f"unknown strategy action: {action}")


def add_strategy_parser(
    resources: argparse._SubParsersAction,
    add_common: Callable[[argparse.ArgumentParser], None],
    handler: Callable[[argparse.Namespace], object],
) -> None:
    strategy = resources.add_parser("strategy")
    actions = strategy.add_subparsers(
        dest="strategy_action", required=True, parser_class=type(strategy)
    )

    listing = actions.add_parser("list")
    listing.add_argument("strategy_id", nargs="?")
    add_common(listing)
    listing.set_defaults(command_handler=handler, command_name="strategy.list")

    info = actions.add_parser("info")
    info.add_argument("strategy_version_id")
    add_common(info)
    info.set_defaults(command_handler=handler, command_name="strategy.info")

    deploy = actions.add_parser("deploy")
    deploy.add_argument("strategy_version_id")
    add_common(deploy)
    deploy.set_defaults(command_handler=handler, command_name="strategy.deploy")

    # Platform-internal batch query used by PTE release validation.
    deployments = actions.add_parser("deployments")
    deployments.add_argument("--release", action="append", required=True)
    add_common(deployments)
    deployments.set_defaults(
        command_handler=handler, command_name="strategy.deployments",
    )
