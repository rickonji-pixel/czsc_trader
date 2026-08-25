from __future__ import annotations

from dataclasses import asdict
import json
import sys

from czsc_trader.application.errors import CommandError
from czsc_trader.application.results import CommandResult


def _write(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def write_result(result: CommandResult) -> int:
    payload = asdict(result)
    payload["warnings"] = list(result.warnings)
    _write(payload)
    return 0


def write_error(command: str, error: CommandError) -> int:
    _write(
        {
            "status": "FAIL",
            "command": command,
            "error": {
                "code": error.code,
                "message": error.message,
                "context": error.context,
            },
            "artifacts": {},
            "warnings": [],
        }
    )
    return error.exit_code
