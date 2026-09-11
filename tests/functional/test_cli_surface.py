from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

from czsc_trader.cli.main import build_parser


EXPECTED_ACTIONS = {
    "data": {"prepare", "prepare-strategy-support", "validate", "update-backtest"},
    "baseline": {"list", "show", "validate"},
    "strategy": {
        "list",
        "validate",
        "show",
        "history",
        "performance",
        "create",
        "version",
        "freeze",
        "promote",
        "downgrade",
        "retire",
        "evidence",
        "evaluate",
        "accept-evaluation",
    },
    "backtest": {"run"},
    "advice": {"run"},
    "archive": {"validate"},
    "chart": {"observation"},
}


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    actions = [
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    assert len(actions) == 1
    return actions[0]


def _command_surface(parser: argparse.ArgumentParser) -> dict[str, set[str]]:
    resources = _subparsers(parser)
    return {
        resource: set(_subparsers(resource_parser).choices)
        for resource, resource_parser in resources.choices.items()
    }


def test_ft_t08_installed_cli_exposes_supported_command_surface() -> None:
    executable = Path(sys.executable).with_name(
        "czsc-trader.exe" if sys.platform == "win32" else "czsc-trader"
    )
    completed = subprocess.run(
        [str(executable), "--help"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stderr == ""
    assert all(resource in completed.stdout for resource in EXPECTED_ACTIONS)
    assert _command_surface(build_parser()) == EXPECTED_ACTIONS
