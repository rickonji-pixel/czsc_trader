from __future__ import annotations

from dataclasses import asdict
import json
import sys

from czsc_trader.application.errors import CommandError
from czsc_trader.application.results import CommandResult


def _write(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def write_result(result: CommandResult, *, output_format: str = "json") -> int:
    payload = asdict(result)
    payload["warnings"] = list(result.warnings)
    if output_format == "text":
        sys.stdout.write(f"{result.status} {result.command}\n")
        sys.stdout.write(json.dumps(payload["result"], ensure_ascii=False, indent=2, default=str) + "\n")
    else:
        _write(payload)
    return 0


def write_error(
    command: str,
    error: CommandError,
    *,
    output_format: str = "json",
) -> int:
    payload = {
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
    if output_format == "text":
        sys.stdout.write(f"FAIL {command}\n{error.code}: {error.message}\n")
    else:
        _write(payload)
    return error.exit_code
