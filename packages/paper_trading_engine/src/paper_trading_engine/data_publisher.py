"""Automatic market-data publication through the czsc-trader CLI boundary."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import shutil
from typing import Callable


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
    ) -> None:
        self.executable = Path(executable)
        self.repo_root = Path(repo_root).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.symbol = symbol.upper()
        self.asset = asset
        self.start_date = start_date
        self.timeout_seconds = timeout_seconds
        self.runner = runner

    def publish(self, end_date: str) -> dict[str, object]:
        arguments = [
            str(self.executable), "data", "prepare",
            "--symbol", self.symbol, "--asset", self.asset,
            "--start", self.start_date, "--end", end_date,
            "--repo-root", str(self.repo_root),
            "--data-dir", str(self.data_dir), "--format", "json",
        ]
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
        return payload["result"]
