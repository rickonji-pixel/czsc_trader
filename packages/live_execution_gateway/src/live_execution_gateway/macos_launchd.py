"""Install S103 simulation, daily recovery, and guarded live jobs as LaunchAgents."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
from typing import Sequence


PTE_LABEL = "com.czsc.s103.pte"
DAILY_LABEL = "com.czsc.s103.longbridge-shadow"
LIVE_LABEL = "com.czsc.s103.longbridge-live"


def _job_payloads(repo_root: Path) -> dict[str, dict[str, object]]:
    logs = repo_root / "state" / "paper_trading_us" / "logs"
    common = {
        "WorkingDirectory": str(repo_root),
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }
    return {
        PTE_LABEL: {
            **common,
            "Label": PTE_LABEL,
            "ProgramArguments": [
                "/bin/zsh", str(repo_root / "scripts" / "pte-us-s103.sh"),
                "serve", "--allow-running", "--no-open",
            ],
            "RunAtLoad": True,
            # The service is intentionally permanent while this LaunchAgent is loaded.
            # Pausing trading is an account operation; it must not stop reconciliation.
            "KeepAlive": True,
            "ThrottleInterval": 30,
            "StandardOutPath": str(logs / "pte-launchd.log"),
            "StandardErrorPath": str(logs / "pte-launchd.error.log"),
        },
        DAILY_LABEL: {
            **common,
            "Label": DAILY_LABEL,
            "ProgramArguments": [
                "/bin/zsh", str(repo_root / "scripts" / "longbridge-us-s103.sh"),
                "daily-if-needed",
            ],
            # Run on login and retry while the Mac is awake. The wrapper does real work
            # only after 06:30 Asia/Shanghai and only until that local day's run succeeds.
            # 06:30 is safely after the US regular-session close in both DST regimes.
            "RunAtLoad": True,
            "StartInterval": 900,
            "ThrottleInterval": 60,
            "StandardOutPath": str(logs / "daily-launchd.log"),
            "StandardErrorPath": str(logs / "daily-launchd.error.log"),
        },
        LIVE_LABEL: {
            **common,
            "Label": LIVE_LABEL,
            "ProgramArguments": [
                "/bin/zsh", str(repo_root / "scripts" / "longbridge-us-s103.sh"), "serve",
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 30,
            "StandardOutPath": str(logs / "longbridge-live.log"),
            "StandardErrorPath": str(logs / "longbridge-live.error.log"),
        },
    }


def render(repo_root: Path, launch_agents_dir: Path) -> list[Path]:
    root = repo_root.resolve()
    required = (
        root / "scripts" / "pte-us-s103.sh",
        root / "scripts" / "longbridge-us-s103.sh",
        root / ".venv" / "bin" / "pte",
        root / ".venv" / "bin" / "longbridge-live",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError("launchd prerequisites are missing: " + ", ".join(missing))
    (root / "state" / "paper_trading_us" / "logs").mkdir(parents=True, exist_ok=True)
    launch_agents_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for label, payload in _job_payloads(root).items():
        path = launch_agents_dir / f"{label}.plist"
        with path.open("wb") as stream:
            plistlib.dump(payload, stream, sort_keys=True)
        paths.append(path)
    return paths


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _bootout(label: str) -> None:
    subprocess.run(
        ["/bin/launchctl", "bootout", f"{_domain()}/{label}"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_unloaded(label: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        completed = subprocess.run(
            ["/bin/launchctl", "print", f"{_domain()}/{label}"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if completed.returncode != 0:
            return
        time.sleep(0.1)
    raise RuntimeError(f"LaunchAgent did not unload within {timeout:.0f}s: {label}")


def install(repo_root: Path, launch_agents_dir: Path, *, load: bool) -> list[Path]:
    paths = render(repo_root, launch_agents_dir)
    if load:
        for path in paths:
            _bootout(path.stem)
        for path in paths:
            _wait_unloaded(path.stem)
        for path in paths:
            label = path.stem
            subprocess.run(["/bin/launchctl", "bootstrap", _domain(), str(path)], check=True)
            subprocess.run(
                ["/bin/launchctl", "enable", f"{_domain()}/{label}"], check=True,
            )
    return paths


def uninstall(launch_agents_dir: Path) -> list[Path]:
    removed = []
    for label in (PTE_LABEL, DAILY_LABEL, LIVE_LABEL):
        _bootout(label)
        path = launch_agents_dir / f"{label}.plist"
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def status() -> int:
    result = 0
    for label in (PTE_LABEL, DAILY_LABEL, LIVE_LABEL):
        print(f"\n[{label}]")
        completed = subprocess.run(
            ["/bin/launchctl", "print", f"{_domain()}/{label}"], check=False,
        )
        result = max(result, completed.returncode)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m live_execution_gateway.macos_launchd")
    parser.add_argument("action", choices=("install", "render", "uninstall", "status"))
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument(
        "--launch-agents-dir", type=Path,
        default=Path.home() / "Library" / "LaunchAgents",
    )
    parser.add_argument("--no-load", action="store_true")
    args = parser.parse_args(argv)
    if sys.platform != "darwin" and args.action != "render":
        raise RuntimeError("S103 LaunchAgents can only be managed on macOS")
    if args.action in {"install", "render"} and args.repo_root is None:
        parser.error("--repo-root is required for install/render")
    if args.action == "render":
        paths = render(args.repo_root, args.launch_agents_dir)
        print("\n".join(str(path) for path in paths))
        return 0
    if args.action == "install":
        paths = install(args.repo_root, args.launch_agents_dir, load=not args.no_load)
        print("installed:\n" + "\n".join(str(path) for path in paths))
        return 0
    if args.action == "uninstall":
        paths = uninstall(args.launch_agents_dir)
        print("removed:\n" + "\n".join(str(path) for path in paths))
        return 0
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
