"""Validated persistent configuration for the Windows service host."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from .runtime_release import RuntimeRelease, resolve_active_release


@dataclass(frozen=True)
class ServiceConfig:
    runtime_root: Path | None = None
    repo_root: Path | None = None
    host: str = "127.0.0.1"
    port: int = 8080

    def __post_init__(self) -> None:
        roots = [root for root in (self.runtime_root, self.repo_root) if root is not None]
        if len(roots) != 1:
            raise ValueError("service config requires exactly one runtime root")
        if not roots[0].is_absolute():
            raise ValueError("service runtime root must be absolute")
        if self.host != "127.0.0.1":
            raise ValueError("service HTTP host must be localhost")

    @property
    def pte_runtime(self) -> bool:
        return self.runtime_root is not None

    @property
    def shared_root(self) -> Path:
        if self.runtime_root is not None:
            return self.runtime_root / "shared"
        assert self.repo_root is not None
        return self.repo_root / "state" / "paper_trading"

    def active_release(self) -> RuntimeRelease | None:
        if self.runtime_root is None:
            return None
        return resolve_active_release(self.runtime_root)

    def _serve_arguments(self, release: RuntimeRelease | None) -> list[str]:
        if release is None:
            assert self.repo_root is not None
            return [
                "serve", "--repo-root", str(self.repo_root), "--host", self.host,
                "--port", str(self.port),
            ]
        return [
            "serve",
            "--repo-root", str(release.release_root),
            "--database", str(self.shared_root / "state" / "runtime.db"),
            "--data-dir", str(self.shared_root / "data"),
            "--config-root", str(self.shared_root / "config"),
            "--advice-executable", str(release.trader_executable),
            "--release-manifest", str(release.manifest_path),
            "--host", self.host,
            "--port", str(self.port),
        ]

    def serve_arguments(self) -> list[str]:
        return self._serve_arguments(self.active_release())

    def pte_command(self) -> list[str]:
        release = self.active_release()
        if release is None:
            assert self.repo_root is not None
            executable = self.repo_root / ".venv" / "Scripts" / "pte.exe"
        else:
            executable = release.pte_executable
        return [str(executable), *self._serve_arguments(release)]

    def working_directory(self) -> Path:
        if self.runtime_root is not None:
            return self.runtime_root
        assert self.repo_root is not None
        return self.repo_root

    @property
    def health_url(self) -> str:
        return f"http://{self.host}:{self.port}/api/health"

    @property
    def log_path(self) -> Path:
        return self.shared_root / "logs" / "pte.log"

    @property
    def watchdog_log_path(self) -> Path:
        return self.shared_root / "logs" / "watchdog.log"

    @property
    def config_path(self) -> Path:
        return self.shared_root / "config" / "service.json"

    def save(self, path: Path | None = None) -> Path:
        destination = path or self.config_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if self.runtime_root is not None:
            payload = {
                "schema_version": 2,
                "runtime_root": str(self.runtime_root),
                "host": self.host,
                "port": self.port,
            }
        else:
            payload = {
                "schema_version": 1,
                "repo_root": str(self.repo_root),
                "host": self.host,
                "port": self.port,
            }
        destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return destination

    @classmethod
    def load(cls, path: Path) -> "ServiceConfig":
        payload = json.loads(path.read_text(encoding="utf-8"))
        schema_version = payload.pop("schema_version", 1)
        if schema_version == 2:
            allowed = {"runtime_root", "host", "port"}
            if set(payload) - allowed:
                raise ValueError("service config contains unsupported fields")
            payload["runtime_root"] = Path(payload["runtime_root"])
            return cls(**payload)
        if schema_version == 1:
            allowed = {"repo_root", "host", "port"}
            if set(payload) - allowed:
                raise ValueError("legacy service config contains unsupported fields")
            payload["repo_root"] = Path(payload["repo_root"])
            return cls(**payload)
        raise ValueError(f"unsupported service config schema: {schema_version}")
