from __future__ import annotations

from collections.abc import Mapping


class CommandError(Exception):
    """Expected command failure with a stable external identity."""

    exit_code = 10

    def __init__(
        self,
        code: str,
        message: str,
        *,
        context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = dict(context or {})


class UsageError(CommandError):
    exit_code = 2


class ValidationError(CommandError):
    exit_code = 3


class SafetyError(CommandError):
    exit_code = 4


class ExecutionError(CommandError):
    exit_code = 5


class InternalError(CommandError):
    exit_code = 10
