"""Shell-free client for the czsc-trader advice CLI."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time
from hashlib import sha256
from typing import Callable

from .contracts import AdviceContractError, AdviceDecision
from .audit import AuditRecorder


class AdviceClientError(RuntimeError):
    """The advice subprocess could not produce a valid decision."""


class CliAdviceClient:
    def __init__(
        self,
        *,
        executable: Path,
        repo_root: Path,
        data_dir: Path,
        symbol: str,
        asset: str,
        timeout_seconds: float = 60,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        audit: AuditRecorder | None = None,
    ) -> None:
        self.executable = Path(executable)
        self.repo_root = Path(repo_root).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.symbol = symbol.upper()
        self.asset = asset
        self.timeout_seconds = float(timeout_seconds)
        self.runner = runner
        self.audit = audit

    def _audit_call(
        self, started: float, *, decision: AdviceDecision | None = None,
        error: Exception | None = None, strategy_id: str | None = None,
        strategy_version: str | None = None,
    ) -> None:
        if self.audit is None:
            return
        correlation_id = decision.decision_id if decision else f"advice:{self.symbol}"
        self.audit.record(
            "EXTERNAL_CALL_FAILED" if error else "EXTERNAL_CALL_SUCCEEDED",
            source="advice_client", outcome="FAILURE" if error else "SUCCESS",
            actor_type="EXTERNAL", actor_id="trader", correlation_id=correlation_id,
            symbol=self.symbol, decision_id=decision.decision_id if decision else None,
            details={
                "service": "trader", "operation": "advice.run",
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                **({"error_type": type(error).__name__, "error": str(error)} if error else {}),
            },
        )
        if error is not None:
            self.audit.record(
                "DECISION_GENERATION_FAILED", source="advice_client", outcome="FAILURE",
                actor_type="ENGINE", correlation_id=correlation_id, symbol=self.symbol,
                strategy_id=strategy_id, strategy_version=strategy_version,
                details={"error_type": type(error).__name__, "error": str(error)},
            )

    def get_decision(
        self,
        actual_quantity: int,
        available_cash: float,
        cycle_target_quantity: int | None = None,
        strategy_id: str | None = None,
        strategy_version: str | None = None,
        baseline: str | None = None,
    ) -> AdviceDecision:
        started = time.perf_counter()
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
            "--available-cash",
            f"{available_cash:.2f}",
        ]
        if cycle_target_quantity is not None:
            arguments.extend(["--cycle-target-quantity", str(cycle_target_quantity)])
        if strategy_id is not None:
            arguments.extend(["--strategy", strategy_id])
            if strategy_version is not None:
                arguments.extend(["--strategy-version", strategy_version])
        elif baseline is not None:
            arguments.extend(["--baseline", baseline])
        arguments.extend(
            [
                "--repo-root",
                str(self.repo_root),
                "--data-dir",
                str(self.data_dir),
                "--format",
                "json",
            ]
        )
        try:
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
                raise AdviceClientError(
                    f"advice command timed out after {self.timeout_seconds:g}s"
                ) from exc
            if completed.returncode != 0:
                raise AdviceClientError(
                    f"advice command exited with exit code {completed.returncode}: "
                    f"{completed.stderr.strip()}"
                )
            lines = completed.stdout.splitlines()
            if len(lines) != 1:
                raise AdviceClientError("advice stdout must contain a single JSON document")
            try:
                payload = json.loads(lines[0])
                decision = AdviceDecision.from_cli_payload(payload)
            except (json.JSONDecodeError, AdviceContractError, KeyError, TypeError, ValueError) as exc:
                raise AdviceClientError(f"invalid advice contract: {exc}") from exc
        except Exception as exc:
            self._audit_call(
                started, error=exc, strategy_id=strategy_id,
                strategy_version=strategy_version,
            )
            raise
        self._audit_call(started, decision=decision)
        return decision

    def data_identity(self) -> str:
        code = self.symbol.split(".", 1)[0]
        names = (
            f"{code}_manifest.json",
            f"{code}_validation.json",
            f"{code}_execution_manifest.json",
        )
        digest = sha256()
        for name in names:
            path = self.data_dir / name
            digest.update(name.encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()
