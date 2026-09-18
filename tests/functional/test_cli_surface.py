from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from czsc_trader.cli.main import build_parser, main


EXPECTED_ACTIONS = {
    "data": {"prepare", "validate", "update-backtest"},
    "baseline": {"list", "show", "validate"},
    "research": {"create", "intent"},
    "strategy": {
        "list",
        "validate",
        "show",
        "history",
        "performance",
        "review",
        "freeze",
        "promote",
        "downgrade",
        "retire",
        "evidence",
    },
    "backtest": {"run"},
    "archive": {"validate"},
    "chart": {"observation"},
    "news": {"extract"},
    "catalog": {"validate", "list", "show"},
    "template": {"validate", "list", "show", "instantiate"},
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


def test_ft_t08_strategy_governance_documented_commands_parse() -> None:
    parser = build_parser()
    documented_commands = (
        [
            "research",
            "create",
            "--input",
            "research-intent.json",
            "--actor",
            "tomxiao",
            "--reason",
            "批准研究立项",
        ],
        [
            "strategy",
            "review",
            "open",
            "--review",
            "FR-S008-C001-001",
            "--candidate",
            "candidate-snapshot.json",
            "--mandate",
            "evaluation-mandate.json",
            "--actor",
            "tomxiao",
        ],
        [
            "strategy",
            "freeze",
            "--strategy",
            "S008",
            "--review",
            "FR-S008-C001-001",
            "--change-summary",
            "首个冻结版本",
            "--actor",
            "tomxiao",
            "--reason",
            "批准低成本模拟观察",
        ],
    )

    parsed = [parser.parse_args(command) for command in documented_commands]

    assert [item.command_name for item in parsed] == [
        "research.create",
        "strategy.review.open",
        "strategy.freeze",
    ]


def test_ft_t08_catalog_cli_validates_lists_and_shows(capsys) -> None:
    repo = Path(__file__).resolve().parents[2]
    root = ["--repo-root", str(repo)]
    for arguments in (
        ["catalog", "validate", *root],
        ["catalog", "list", "--kind", "factor", "--status", "READY", *root],
        ["catalog", "show", "--id", "F-PROJECT-ER60", *root],
    ):
        assert main(arguments) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "PASS"
    assert payload["result"]["definition"]["factor_id"] == "F-PROJECT-ER60"


def test_ft_t08_template_cli_validates_lists_shows_and_instantiates(capsys, tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    root = ["--repo-root", str(repo)]
    for arguments in (
        ["template", "validate", *root],
        ["template", "list", "--status", "READY", *root],
        ["template", "show", "--id", "STC-T04-EVENT-HOLD", *root],
    ):
        assert main(arguments) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "PASS"
    assert payload["result"]["template"]["operator"] == "EVENT_HOLD"

    spec = tmp_path / "prototype.json"
    spec.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "template_id": "STC-T04-EVENT-HOLD",
                "bindings": [
                    {
                        "slot": "entry_events",
                        "source_id": "SIG-CZSC-cxt_bi_base_V230228",
                        "source_kind": "SIGNAL",
                        "state": "满足",
                        "weight": None,
                    }
                ],
                "parameters": {"holding_sessions": 5},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert main(["template", "instantiate", "--spec", str(spec), *root]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "PASS"
    assert payload["result"]["instance"]["instance_id"].startswith("STI-")

    invalid = json.loads(spec.read_text(encoding="utf-8"))
    invalid["bindings"][0]["source_id"] = "SIG-NOT-IN-FSC"
    spec.write_text(json.dumps(invalid), encoding="utf-8")
    assert main(["template", "instantiate", "--spec", str(spec), *root]) != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "FAIL"
    assert payload["error"]["code"] == "strategy_template_binding_invalid"
