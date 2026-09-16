"""Automatic market-data publication through the czsc-trader CLI boundary."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import shutil
import time
from typing import Callable

from .audit import AuditRecorder


class DataPublicationError(RuntimeError):
    pass


def seed_runtime_data(source: Path, target: Path, symbol: str) -> None:
    """Copy the tracked validated generation once as a runtime bootstrap."""
    code = symbol.upper().split(".", 1)[0]
    target = Path(target)
    if (target / f"{code}_manifest.json").is_file():
        return
    target.mkdir(parents=True, exist_ok=True)
    for path in Path(source).glob(f"{code}_*"):
        if path.is_file():
            shutil.copy2(path, target / path.name)


class CliDataPublisher:
    def __init__(
        self,
        *,
        executable: Path,
        repo_root: Path,
        data_dir: Path,
        symbol: str,
        asset: str,
        start_date: str,
        timeout_seconds: float = 600,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        audit: AuditRecorder | None = None,
    ) -> None:
        self.executable = Path(executable)
        self.repo_root = Path(repo_root).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.symbol = symbol.upper()
        self.asset = asset
        self.start_date = start_date
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        self.audit = audit

    def _audit_call(self, end_date: str, started: float, error: Exception | None = None) -> None:
        if self.audit is None:
            return
        self.audit.record(
            "EXTERNAL_CALL_FAILED" if error else "EXTERNAL_CALL_SUCCEEDED",
            source="data_publisher", outcome="FAILURE" if error else "SUCCESS",
            actor_type="EXTERNAL", actor_id="trader",
            correlation_id=f"publication:{end_date}", symbol=self.symbol,
            details={
                "service": "trader", "upstream_service": "tushare",
                "operation": "data.prepare",
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                **({"error_type": type(error).__name__, "error": str(error)} if error else {}),
            },
        )

    def publish(self, end_date: str) -> dict[str, object]:
        started = time.perf_counter()
        arguments = [
            str(self.executable), "data", "prepare",
            "--symbol", self.symbol, "--asset", self.asset,
            "--start", self.start_date, "--end", end_date,
            "--repo-root", str(self.repo_root),
            "--data-dir", str(self.data_dir), "--format", "json",
        ]
        try:
            try:
                completed = self.runner(
                    arguments, cwd=self.repo_root, check=False, capture_output=True,
                    text=True, encoding="utf-8", timeout=self.timeout_seconds, shell=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise DataPublicationError("data publication timed out") from exc
            if completed.returncode != 0:
                detail = completed.stderr.strip()
                try:
                    payload = json.loads(completed.stdout)
                    machine_message = payload.get("error", {}).get("message")
                    if isinstance(machine_message, str) and machine_message.strip():
                        detail = machine_message.strip()
                except (json.JSONDecodeError, AttributeError):
                    pass
                raise DataPublicationError(
                    f"data publication failed with exit code {completed.returncode}: {detail}"
                )
            if len(completed.stdout.splitlines()) != 1:
                raise DataPublicationError("data publication stdout must contain one JSON document")
            try:
                payload = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise DataPublicationError("data publication returned invalid JSON") from exc
            if payload.get("status") != "PASS" or not isinstance(payload.get("result"), dict):
                raise DataPublicationError("data publication command did not pass")
        except Exception as exc:
            self._audit_call(end_date, started, exc)
            raise
        self._audit_call(end_date, started)
        return payload["result"]


class AccountDataPublisher:
    """Publish every distinct instrument currently owned by a virtual account."""

    def __init__(
        self,
        *,
        store,
        executable: Path,
        repo_root: Path,
        data_dir: Path,
        start_date: str,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        audit: AuditRecorder | None = None,
    ) -> None:
        self.store = store
        self.executable = executable
        self.repo_root = repo_root
        self.data_dir = data_dir
        self.start_date = start_date
        self.runner = runner
        self.audit = audit

    def publish_release(
        self,
        symbol: str,
        asset: str,
        releases: list[tuple[str, str]],
        end_date: str,
    ) -> dict[str, object]:
        """Ask TDR to atomically publish one instrument and all bound releases."""
        started = time.perf_counter()
        arguments = [
            str(self.executable), "data", "publish-runtime",
            "--symbol", symbol.upper(), "--asset", asset,
            "--start", self.start_date, "--through", end_date,
            "--repo-root", str(self.repo_root), "--data-dir", str(self.data_dir),
            "--format", "json",
        ]
        for strategy_id, strategy_version in sorted(set(releases)):
            arguments.extend(["--release", f"{strategy_id}:{strategy_version}"])
        try:
            try:
                completed = self.runner(
                    arguments, cwd=self.repo_root, check=False, capture_output=True,
                    text=True, encoding="utf-8", timeout=600, shell=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise DataPublicationError("runtime data publication timed out") from exc
            if completed.returncode != 0:
                detail = completed.stderr.strip()
                try:
                    payload = json.loads(completed.stdout)
                    message = payload.get("error", {}).get("message")
                    if isinstance(message, str) and message.strip():
                        detail = message.strip()
                except (json.JSONDecodeError, AttributeError):
                    pass
                raise DataPublicationError(
                    f"runtime data publication failed with exit code {completed.returncode}: {detail}"
                )
            if len(completed.stdout.splitlines()) != 1:
                raise DataPublicationError("runtime data publication stdout must contain one JSON document")
            payload = json.loads(completed.stdout)
            if payload.get("status") != "PASS" or not isinstance(payload.get("result"), dict):
                raise DataPublicationError("runtime data publication command did not pass")
            result = payload["result"]
            cutoff = result.get("data_cutoff")
            generation_id = result.get("generation_id")
            if not isinstance(cutoff, str) or not cutoff or not isinstance(generation_id, str) or not generation_id:
                raise DataPublicationError("runtime data publication result is missing identity")
        except Exception as exc:
            if self.audit is not None:
                self.audit.record(
                    "EXTERNAL_CALL_FAILED", source="data_publisher", outcome="FAILURE",
                    actor_type="EXTERNAL", actor_id="trader", symbol=symbol.upper(),
                    correlation_id=f"publication:{end_date}",
                    details={
                        "service": "trader", "upstream_service": "tushare",
                        "operation": "data.publish-runtime",
                        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                        "error_type": type(exc).__name__, "error": str(exc),
                    },
                )
            raise
        if self.audit is not None:
            self.audit.record(
                "EXTERNAL_CALL_SUCCEEDED", source="data_publisher",
                actor_type="EXTERNAL", actor_id="trader", symbol=symbol.upper(),
                correlation_id=f"publication:{end_date}",
                details={
                    "service": "trader", "upstream_service": "tushare",
                    "operation": "data.publish-runtime",
                    "generation_id": result["generation_id"],
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                },
            )
        return result

    def publish(self, end_date: str) -> dict[str, object]:
        accounts = [
            account for account in self.store.virtual_accounts()
            if account.get("status") != "RETIRED"
        ]
        if not accounts:
            raise DataPublicationError("no active virtual-account instrument to publish")
        releases_by_instrument: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for account in accounts:
            instrument = (str(account["symbol"]).upper(), str(account["asset_type"]))
            releases_by_instrument.setdefault(instrument, []).append(
                (str(account["strategy_id"]), str(account["strategy_version"]))
            )
        published = []
        cutoffs = set()
        for (symbol, asset), releases in sorted(releases_by_instrument.items()):
            result = self.publish_release(symbol, asset, releases, end_date)
            cutoff = result.get("data_cutoff")
            if not isinstance(cutoff, str) or not cutoff:
                raise DataPublicationError(f"{symbol}: publication result missing data_cutoff")
            cutoffs.add(cutoff)
            published.append({"symbol": symbol, "asset_type": asset, "result": result})
        if len(cutoffs) != 1:
            raise DataPublicationError("published instruments have different data cutoffs")
        data_cutoff = cutoffs.pop()
        return {
            "data_cutoff": data_cutoff,
            "instruments": published,
            "generation_ids": [item["result"]["generation_id"] for item in published],
        }
