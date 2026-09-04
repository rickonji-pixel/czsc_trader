"""Validated persistent configuration for the Windows service host."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class ServiceConfig:
    repo_root: Path
    host: str = "127.0.0.1"
    port: int = 8080

    def __post_init__(self) -> None:
        if not self.repo_root.is_absolute():
            raise ValueError("service repo_root must be absolute")
        if self.host != "127.0.0.1":
            raise ValueError("service HTTP host must be localhost")

    def serve_arguments(self) -> list[str]:
        return [
            "serve", "--repo-root", str(self.repo_root), "--host", self.host,
            "--port", str(self.port),
        ]

    def pte_command(self) -> list[str]:
        return [
            str(self.repo_root / ".venv" / "Scripts" / "pte.exe"),
            *self.serve_arguments(),
        ]

    @property
    def health_url(self) -> str:
        return f"http://{self.host}:{self.port}/api/status"

    @property
    def log_path(self) -> Path:
        return self.repo_root / "state" / "paper_trading" / "logs" / "pte.log"

    @property
    def watchdog_log_path(self) -> Path:
        return self.repo_root / "state" / "paper_trading" / "logs" / "watchdog.log"

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {**asdict(self), "repo_root": str(self.repo_root)}
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "ServiceConfig":
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["repo_root"] = Path(payload["repo_root"])
        return cls(**payload)
