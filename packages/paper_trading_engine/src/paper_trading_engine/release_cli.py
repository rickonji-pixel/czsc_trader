"""Build and select immutable PTE runtime releases."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from typing import Any, Callable, Sequence
from urllib.request import urlopen
from uuid import uuid4

from .runtime_release import (
    MANIFEST_NAME,
    RELEASE_ID_PATTERN,
    activate_release,
    active_release_path,
    editable_installations,
    file_sha256,
    load_release,
    resolve_active_release,
    service_host_runtime_dependencies,
    tree_sha256,
)
from .store import (
    RUNTIME_DATABASE_COMPATIBLE_VERSIONS,
    RUNTIME_DATABASE_SCHEMA_VERSION,
)


PTE_LOCAL_PROJECTS = (
    "packages/dataflows",
    "packages/strategy_manager",
    "packages/strategy_runtime",
    ".",
    "packages/paper_trading_engine",
)
PTE_RESOLUTION_DISTRIBUTIONS = (
    "paper-trading-engine",
    "czsc-strategy-runtime",
    "czsc-dataflows",
    "czsc-strategy-manager",
)


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _run(
    command: Sequence[str], *, cwd: Path, runner: Runner = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    completed = runner(
        list(command), cwd=cwd, check=False, capture_output=True, text=True,
        encoding="utf-8",
    )
    if completed.returncode:
        detail = "\n".join(
            part for part in (completed.stdout.strip(), completed.stderr.strip()) if part
        )
        raise RuntimeError(f"release command failed: {' '.join(command)}: {detail}")
    return completed


def _git_release_identity(repo_root: Path, release_id: str, runner: Runner) -> str:
    status = _run(["git", "status", "--porcelain"], cwd=repo_root, runner=runner)
    if status.stdout.strip():
        raise RuntimeError("PTE release requires a clean Git worktree")
    commit = _run(["git", "rev-parse", "HEAD"], cwd=repo_root, runner=runner).stdout.strip()
    tag_commit = _run(
        ["git", "rev-list", "-n", "1", release_id], cwd=repo_root, runner=runner,
    ).stdout.strip()
    if tag_commit != commit:
        raise RuntimeError(f"PTE release tag {release_id} does not identify HEAD")
    return commit


def _python_in(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _create_environment(
    *,
    source_python: Path,
    venv: Path,
    artifacts: Path,
    repo_root: Path,
    runner: Runner,
    packages: Sequence[str] = (),
    no_dependencies: Sequence[str] = (),
) -> None:
    _run([str(source_python), "-m", "venv", str(venv)], cwd=repo_root, runner=runner)
    python = _python_in(venv)
    for dependencies, selected in ((True, packages), (False, no_dependencies)):
        if not selected:
            continue
        command = [
            str(python), "-m", "pip", "install", "--no-index",
            "--find-links", str(artifacts),
        ]
        if not dependencies:
            command.append("--no-deps")
        _run([*command, *selected], cwd=repo_root, runner=runner)


def _wheel_path(artifacts: Path, distribution: str) -> Path:
    normalized = distribution.lower().replace("-", "_")
    matches = sorted(artifacts.glob(f"{normalized}-*.whl"))
    if len(matches) != 1:
        raise RuntimeError(
            f"PTE release requires exactly one {distribution} wheel; found {len(matches)}"
        )
    return matches[0]


def _build_pte_wheelhouse(
    *,
    source_python: Path,
    artifacts: Path,
    constraints: Path,
    repo_root: Path,
    runner: Runner,
) -> None:
    wheels = artifacts.parent / ".dependency-wheels"
    wheels.mkdir()
    try:
        roots = [
            str(_wheel_path(artifacts, distribution))
            for distribution in PTE_RESOLUTION_DISTRIBUTIONS
        ]
        _run(
            [
                str(source_python), "-m", "pip", "wheel",
                "--wheel-dir", str(wheels), "--constraint", str(constraints),
                *roots,
            ],
            cwd=repo_root,
            runner=runner,
        )
        for source in sorted(wheels.iterdir()):
            target = artifacts / source.name
            if target.exists():
                if file_sha256(source) != file_sha256(target):
                    raise RuntimeError(f"PTE dependency wheel identity differs: {source.name}")
                continue
            source.replace(target)
    finally:
        shutil.rmtree(wheels)


def _verify_installed_environment(
    release_root: Path, *, repo_root: Path, runner: Runner,
) -> None:
    script = (
        "import importlib.metadata as m,json\n"
        "import paper_trading_engine.cli\n"
        "from czsc_trader.application.strategy_service import show_strategy_deployments\n"
        "from czsc_trader.observation_chart import render_observation_html\n"
        "expected=('paper-trading-engine','czsc-trader-research','czsc-strategy-runtime',"
        "'czsc-strategy-manager','czsc-dataflows')\n"
        "forbidden=('vectorbt','optuna','tsfresh','czsc-strategy-evaluator',"
        "'czsc-factor-signal-catalog','czsc-strategy-template-catalog',"
        "'czsc-trading-execution-engine')\n"
        "editable=[]\n"
        "for name in expected:\n"
        "    direct=m.distribution(name).read_text('direct_url.json')\n"
        "    if json.loads(direct or '{}').get('dir_info',{}).get('editable'):\n"
        "        editable.append(name)\n"
        "present=[]\n"
        "for name in forbidden:\n"
        "    try: m.distribution(name)\n"
        "    except m.PackageNotFoundError: continue\n"
        "    present.append(name)\n"
        "assert not editable, f'editable PTE packages: {editable}'\n"
        "assert not present, f'RSCH-only packages installed in PTE: {present}'\n"
    )
    _run(
        [str(_python_in(release_root / ".venv")), "-c", script],
        cwd=repo_root,
        runner=runner,
    )


def _write_manifest(
    *,
    release_root: Path,
    release_id: str,
    git_commit: str,
    runtime_files: dict[str, str],
) -> dict[str, Any]:
    artifacts = {
        path.name: file_sha256(path)
        for path in sorted((release_root / "artifacts").glob("*.whl"))
    }
    manifest = {
        "schema_version": 1,
        "release_id": release_id,
        "git_commit": git_commit,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "python_version": sys.version.split()[0],
        "database_schema": {
            "current": RUNTIME_DATABASE_SCHEMA_VERSION,
            "compatible": list(RUNTIME_DATABASE_COMPATIBLE_VERSIONS),
        },
        "artifacts": artifacts,
        "strategy_snapshot_sha256": tree_sha256(release_root / "strategies"),
        "runtime_files": dict(sorted(runtime_files.items())),
    }
    (release_root / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def _write_runtime_root(release_root: Path) -> dict[str, str]:
    marker = release_root / "runtime-root.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "czsc-trader-runtime",
                "strategy_root": "strategies",
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return {marker.name: file_sha256(marker)}


def _initialize_service_host(
    runtime_root: Path,
    release_id: str,
    artifacts: Path,
    source_python: Path,
    repo_root: Path,
    runner: Runner,
) -> None:
    host = runtime_root / "host" / "releases" / release_id
    if host.exists():
        raise RuntimeError(f"PTE service host already exists: {host}")
    host.parent.mkdir(parents=True, exist_ok=True)
    host.mkdir()
    try:
        _create_environment(
            source_python=source_python,
            venv=host / ".venv",
            artifacts=artifacts,
            repo_root=repo_root,
            runner=runner,
            packages=(("pywin32>=308",) if os.name == "nt" else ()),
            no_dependencies=("paper-trading-engine==0.1.0",),
        )
        _verify_service_host(host)
    except Exception:
        shutil.rmtree(host)
        raise


def _verify_service_host(host: Path) -> None:
    scripts = host / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    watchdog = scripts / ("pte-watchdog.exe" if os.name == "nt" else "pte-watchdog")
    if not watchdog.is_file():
        raise RuntimeError(f"PTE service host is incomplete: {watchdog}")
    site_packages = host / ".venv" / "Lib" / "site-packages"
    if not site_packages.is_dir():
        candidates = sorted((host / ".venv" / "lib").glob("python*/site-packages"))
        if len(candidates) != 1:
            raise RuntimeError(f"PTE service host has no site-packages: {host}")
        site_packages = candidates[0]
    editable = editable_installations(site_packages)
    if editable:
        raise RuntimeError(f"PTE service host contains editable installations: {editable}")
    heavyweight = service_host_runtime_dependencies(site_packages)
    if heavyweight:
        raise RuntimeError(f"PTE service host contains runtime dependencies: {heavyweight}")


def assemble_release(
    *,
    repo_root: Path,
    runtime_root: Path,
    release_id: str,
    source_python: Path = Path(sys.executable),
    runner: Runner = subprocess.run,
) -> dict[str, object]:
    repo_root = repo_root.resolve()
    runtime_root = runtime_root.resolve()
    if not RELEASE_ID_PATTERN.fullmatch(release_id):
        raise RuntimeError(f"invalid PTE release id: {release_id}")
    git_commit = _git_release_identity(repo_root, release_id, runner)
    releases = runtime_root / "releases"
    destination = releases / release_id
    if destination.exists():
        raise RuntimeError(f"PTE release already exists: {destination}")
    releases.mkdir(parents=True, exist_ok=True)
    staging = releases / f".{release_id}-{uuid4().hex}"
    staging.mkdir()
    try:
        artifacts = staging / "artifacts"
        artifacts.mkdir()
        frozen = _run(
            [
                str(source_python), "-m", "pip", "freeze", "--exclude-editable",
                *[
                    option
                    for distribution in (*PTE_RESOLUTION_DISTRIBUTIONS, "czsc-trader-research")
                    for option in ("--exclude", distribution)
                ],
            ],
            cwd=repo_root,
            runner=runner,
        ).stdout
        constraints = staging / "build-constraints.txt"
        constraints.write_text(frozen, encoding="utf-8")
        for project in PTE_LOCAL_PROJECTS:
            _run(
                [
                    str(source_python), "-m", "pip", "wheel", "--no-deps",
                    "--wheel-dir", str(artifacts), str(repo_root / project),
                ],
                cwd=repo_root,
                runner=runner,
            )
        _build_pte_wheelhouse(
            source_python=source_python,
            artifacts=artifacts,
            constraints=constraints,
            repo_root=repo_root,
            runner=runner,
        )
        shutil.copytree(
            repo_root / "strategies",
            staging / "strategies",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.lock"),
        )
        runtime_files = {
            constraints.name: file_sha256(constraints),
            **_write_runtime_root(staging),
        }
        staging.replace(destination)
        try:
            _create_environment(
                source_python=source_python,
                venv=destination / ".venv",
                artifacts=destination / "artifacts",
                repo_root=repo_root,
                runner=runner,
                packages=(
                    "paper-trading-engine==0.1.0", "czsc-strategy-manager==0.1.0",
                ),
                no_dependencies=("czsc-trader-research==0.1.0",),
            )
            environment_lock = destination / "environment.lock"
            environment_lock.write_text(
                _run(
                    [str(_python_in(destination / ".venv")), "-m", "pip", "freeze", "--all"],
                    cwd=repo_root,
                    runner=runner,
                ).stdout,
                encoding="utf-8",
            )
            runtime_files[environment_lock.name] = file_sha256(environment_lock)
            manifest = _write_manifest(
                release_root=destination,
                release_id=release_id,
                git_commit=git_commit,
                runtime_files=runtime_files,
            )
            _verify_installed_environment(destination, repo_root=repo_root, runner=runner)
            release = load_release(runtime_root, release_id)
        except Exception:
            shutil.rmtree(destination)
            raise
        try:
            for directory in ("config", "data", "logs", "state"):
                (runtime_root / "shared" / directory).mkdir(parents=True, exist_ok=True)
            _initialize_service_host(
                runtime_root,
                release_id,
                release.release_root / "artifacts",
                source_python,
                repo_root,
                runner,
            )
        except Exception:
            shutil.rmtree(destination)
            raise
        return {
            "release": release.identity(),
            "artifact_count": len(manifest["artifacts"]),
            "active": False,
        }
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _write(payload: object) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _restart_command(
    runtime_root: Path, release_id: str, wait: float, host: str, port: int,
) -> list[str]:
    release = load_release(runtime_root, release_id)
    shared = runtime_root / "shared"
    return [
        str(release.pte_executable),
        "control", "restart",
        "--repo-root", str(release.release_root),
        "--database", str(shared / "state" / "runtime.db"),
        "--data-dir", str(shared / "data"),
        "--config-root", str(shared / "config"),
        "--release-manifest", str(release.manifest_path),
        "--host", host,
        "--port", str(port),
        "--wait", str(wait),
    ]


def _running_release(host: str, port: int, timeout: float = 5.0) -> str | None:
    with urlopen(f"http://{host}:{port}/api/health", timeout=timeout) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    release = payload.get("release")
    if not isinstance(release, dict):
        return None
    value = release.get("release_id")
    return str(value) if value else None


def _database_schema_version(database: Path) -> int:
    if not database.is_file():
        return RUNTIME_DATABASE_SCHEMA_VERSION
    connection = sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT value FROM settings WHERE key='runtime_database_schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        connection.close()
    if row is None:
        return RUNTIME_DATABASE_SCHEMA_VERSION
    try:
        return int(row[0])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"PTE runtime database has an invalid schema version: {row[0]!r}"
        ) from exc


def _require_database_compatibility(release, database: Path) -> int:
    database_schema = _database_schema_version(database)
    compatible = release.manifest["database_schema"]["compatible"]
    if database_schema not in compatible:
        raise RuntimeError(
            f"PTE release {release.release_id} does not support database schema "
            f"{database_schema}; compatible={compatible}"
        )
    return database_schema


def deploy_release(
    runtime_root: Path,
    release_id: str,
    *,
    wait: float = 60.0,
    host: str = "127.0.0.1",
    port: int = 8080,
    runner: Runner = subprocess.run,
    running_release: Callable[[str, int], str | None] = _running_release,
) -> dict[str, object]:
    if host != "127.0.0.1":
        raise RuntimeError("PTE deployment host must be localhost")
    runtime_root = runtime_root.resolve()
    previous = resolve_active_release(runtime_root)
    target = load_release(runtime_root, release_id)
    if previous.release_id == release_id:
        raise RuntimeError(f"PTE release is already active: {release_id}")
    database = runtime_root / "shared" / "state" / "runtime.db"
    _require_database_compatibility(target, database)
    activate_release(runtime_root, release_id)
    try:
        _run(
            _restart_command(runtime_root, previous.release_id, wait, host, port),
            cwd=runtime_root,
            runner=runner,
        )
        observed = running_release(host, port)
        if observed != release_id:
            raise RuntimeError(
                f"PTE health reported release {observed!r}, expected {release_id!r}"
            )
    except Exception as deployment_error:
        _require_database_compatibility(previous, database)
        activate_release(runtime_root, previous.release_id)
        try:
            _run(
                _restart_command(runtime_root, previous.release_id, wait, host, port),
                cwd=runtime_root,
                runner=runner,
            )
            observed = running_release(host, port)
            if observed != previous.release_id:
                raise RuntimeError(
                    f"rollback health reported release {observed!r}, "
                    f"expected {previous.release_id!r}"
                )
        except Exception as rollback_error:
            raise RuntimeError(
                f"PTE deployment failed ({deployment_error}); rollback failed ({rollback_error})"
            ) from rollback_error
        raise RuntimeError(
            f"PTE deployment failed and was rolled back: {deployment_error}"
        ) from deployment_error
    return {
        "status": "READY",
        "release_id": release_id,
        "previous_release_id": previous.release_id,
    }


def deploy_previous_release(
    runtime_root: Path,
    *,
    wait: float = 60.0,
    host: str = "127.0.0.1",
    port: int = 8080,
    runner: Runner = subprocess.run,
    running_release: Callable[[str, int], str | None] = _running_release,
) -> dict[str, object]:
    selection = json.loads(active_release_path(runtime_root).read_text(encoding="utf-8"))
    previous = selection.get("previous_release_id")
    if not isinstance(previous, str) or not previous:
        raise RuntimeError("active PTE release has no rollback target")
    return deploy_release(
        runtime_root,
        previous,
        wait=wait,
        host=host,
        port=port,
        runner=runner,
        running_release=running_release,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pte-release")
    actions = parser.add_subparsers(dest="action", required=True)
    assemble = actions.add_parser("assemble")
    assemble.add_argument("--repo-root", required=True, type=Path)
    assemble.add_argument("--runtime-root", required=True, type=Path)
    assemble.add_argument("--release", required=True)
    assemble.add_argument("--python", type=Path, default=Path(sys.executable))
    for action in ("activate", "deploy", "rollback", "status"):
        leaf = actions.add_parser(action)
        leaf.add_argument("--runtime-root", required=True, type=Path)
        if action in {"activate", "deploy"}:
            leaf.add_argument("--release", required=True)
        if action in {"deploy", "rollback"}:
            leaf.add_argument("--wait", type=float, default=60.0)
            leaf.add_argument("--host", default="127.0.0.1")
            leaf.add_argument("--port", type=int, default=8080)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "assemble":
            result = assemble_release(
                repo_root=args.repo_root,
                runtime_root=args.runtime_root,
                release_id=args.release,
                source_python=args.python,
            )
        elif args.action == "activate":
            release = load_release(args.runtime_root, args.release)
            _require_database_compatibility(
                release, args.runtime_root / "shared" / "state" / "runtime.db"
            )
            result = activate_release(args.runtime_root, args.release)
        elif args.action == "deploy":
            result = deploy_release(
                args.runtime_root,
                args.release,
                wait=args.wait,
                host=args.host,
                port=args.port,
            )
        elif args.action == "rollback":
            result = deploy_previous_release(
                args.runtime_root,
                wait=args.wait,
                host=args.host,
                port=args.port,
            )
        else:
            active = resolve_active_release(args.runtime_root)
            result = {
                "active": active.identity(),
                "selection": json.loads(
                    active_release_path(args.runtime_root).read_text(encoding="utf-8")
                ),
            }
        _write({"status": "PASS", "command": f"pte.release.{args.action}", "result": result})
        return 0
    except Exception as exc:
        _write({
            "status": "FAIL",
            "command": f"pte.release.{args.action}",
            "error": {"code": "release_error", "message": str(exc)},
        })
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
