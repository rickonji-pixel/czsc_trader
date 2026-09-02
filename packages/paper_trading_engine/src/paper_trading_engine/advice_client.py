"""Shell-free client for the czsc-trader advice CLI."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Callable

from .contracts import AdviceContractError, AdviceDecision


class AdviceClientError(RuntimeError):
    """The advice subprocess could not produce a valid decision."""


class CliAdviceClient:
    def __init__(
        self,
        *,
        executable: Path,
        repo_root: Path,
        symbol: str,
        asset: str,
        position_size: int,
        timeout_seconds: float = 60,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.executable = Path(executable)
        self.repo_root = Path(repo_root).resolve()
        self.symbol = symbol.upper()
        self.asset = asset
        self.position_size = int(position_size)
        self.timeout_seconds = float(timeout_seconds)
        self.runner = runner

    def get_decision(self, actual_quantity: int) -> AdviceDecision:
        arguments = [
            str(self.executable),
            "advice",
            "run",
            "--symbol",
            self.symbol,
            "--asset",
            self.asset,
            "--actual-quantity",
            str(actual_quantity),
            "--position-size",
            str(self.position_size),
            "--repo-root",
            str(self.repo_root),
            "--format",
            "json",
        ]
        try:
            completed = self.runner(
                arguments,
                cwd=self.repo_root,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdviceClientError(f"advice command timed out after {self.timeout_seconds:g}s") from exc
        if completed.returncode != 0:
            raise AdviceClientError(
                f"advice command exited with exit code {completed.returncode}: {completed.stderr.strip()}"
            )
        lines = completed.stdout.splitlines()
        if len(lines) != 1:
            raise AdviceClientError("advice stdout must contain a single JSON document")
        try:
            payload = json.loads(lines[0])
            return AdviceDecision.from_cli_payload(payload)
        except (json.JSONDecodeError, AdviceContractError, KeyError, TypeError, ValueError) as exc:
            raise AdviceClientError(f"invalid advice contract: {exc}") from exc
