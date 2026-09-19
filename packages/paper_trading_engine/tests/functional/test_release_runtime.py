from pathlib import Path
import subprocess

import pytest

from paper_trading_engine.release_cli import PTE_LOCAL_PROJECTS, assemble_release
from paper_trading_engine.runtime_release import load_release
from czsc_trader.application.context import RepositoryContext


def test_release_assembly_creates_venvs_at_final_paths(tmp_path):
    repo = (tmp_path / "repo").resolve()
    runtime = (tmp_path / "runtime").resolve()
    strategies = repo / "strategies"
    strategies.mkdir(parents=True)
    (strategies / "registry.json").write_text(
        '{"schema_version":1,"strategies":[]}', encoding="utf-8",
    )
    commands = []
    wheel_index = 0

    def run(command, **_kwargs):
        nonlocal wheel_index
        commands.append(list(command))
        if command[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:3] in (["git", "rev-parse", "HEAD"], ["git", "rev-list", "-n"]):
            return subprocess.CompletedProcess(command, 0, stdout="a" * 40 + "\n", stderr="")
        if command[1:5] == ["-m", "pip", "freeze", "--exclude-editable"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[1:5] == ["-m", "pip", "freeze", "--all"]:
            return subprocess.CompletedProcess(
                command, 0, stdout="paper-trading-engine==0.1.0\n", stderr="",
            )
        if command[1:4] == ["-m", "pip", "wheel"] and "--constraint" in command:
            Path(command[command.index("--wheel-dir") + 1]).mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[1:4] == ["-m", "pip", "wheel"]:
            destination = Path(command[command.index("--wheel-dir") + 1])
            destination.mkdir(parents=True, exist_ok=True)
            prefixes = (
                "czsc_dataflows", "czsc_strategy_manager", "czsc_strategy_runtime",
                "czsc_trader_research", "paper_trading_engine",
            )
            (destination / f"{prefixes[wheel_index]}-0.1.0-py3-none-any.whl").write_bytes(
                b"wheel"
            )
            wheel_index += 1
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[1:4] == ["-m", "venv", command[-1]]:
            venv = Path(command[-1])
            scripts = venv / "Scripts"
            scripts.mkdir(parents=True)
            (scripts / "python.exe").write_bytes(b"python")
            (venv / "Lib" / "site-packages").mkdir(parents=True)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[1:4] == ["-m", "pip", "install"]:
            scripts = Path(command[0]).parent
            (scripts / "pte.exe").write_bytes(b"pte")
            (scripts / "czsc-trader.exe").write_bytes(b"trader")
            (scripts / "pte-watchdog.exe").write_bytes(b"watchdog")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[1] == "-c":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(command)

    result = assemble_release(
        repo_root=repo,
        runtime_root=runtime,
        release_id="v0.4.1",
        source_python=Path("C:/Python/python.exe"),
        runner=run,
    )

    release = load_release(runtime, "v0.4.1")
    context = RepositoryContext.discover(
        release.release_root, explicit_root=release.release_root,
    )
    assert result["release"]["release_id"] == "v0.4.1"
    assert result["artifact_count"] == len(PTE_LOCAL_PROJECTS)
    assert release.pte_executable.is_file()
    assert context.strategy_root == release.release_root / "strategies"
    assert (release.release_root / "environment.lock").read_text(encoding="utf-8") == (
        "paper-trading-engine==0.1.0\n"
    )
    assert (
        runtime / "host" / "releases" / "v0.4.1" / ".venv" / "Scripts"
        / "pte-watchdog.exe"
    ).is_file()
    venv_targets = [Path(command[-1]) for command in commands if command[1:3] == ["-m", "venv"]]
    assert venv_targets == [
        runtime / "releases" / "v0.4.1" / ".venv",
        runtime / "host" / "releases" / "v0.4.1" / ".venv",
    ]
    installs = [command for command in commands if command[1:4] == ["-m", "pip", "install"]]
    assert any("--no-deps" in command and "czsc-trader-research==0.1.0" in command
               for command in installs)
    dependency_commands = [
        command for command in commands
        if command[1:4] == ["-m", "pip", "install"] or "--constraint" in command
    ]
    assert all("vectorbt" not in " ".join(command).lower() for command in dependency_commands)
    wheelhouse = next(
        command for command in commands if "--constraint" in command
    )
    assert "czsc_trader_research" not in " ".join(wheelhouse).lower()
    assert not any(path.name.startswith(".v0.4.1-") for path in (runtime / "releases").iterdir())


def test_release_assembly_rejects_incomplete_existing_service_host(tmp_path):
    repo = (tmp_path / "repo").resolve()
    runtime = (tmp_path / "runtime").resolve()
    (repo / "strategies").mkdir(parents=True)
    (runtime / "host" / "releases" / "v0.4.1").mkdir(parents=True)

    def run(command, **_kwargs):
        if command[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:3] in (["git", "rev-parse", "HEAD"], ["git", "rev-list", "-n"]):
            return subprocess.CompletedProcess(command, 0, stdout="a" * 40 + "\n", stderr="")
        if command[1:5] == ["-m", "pip", "freeze", "--exclude-editable"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[1:5] == ["-m", "pip", "freeze", "--all"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[1:4] == ["-m", "pip", "wheel"] and "--constraint" in command:
            Path(command[command.index("--wheel-dir") + 1]).mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[1:4] == ["-m", "pip", "wheel"]:
            destination = Path(command[command.index("--wheel-dir") + 1])
            destination.mkdir(parents=True, exist_ok=True)
            prefixes = (
                "czsc_dataflows", "czsc_strategy_manager", "czsc_strategy_runtime",
                "czsc_trader_research", "paper_trading_engine",
            )
            index = len(list(destination.glob("*.whl")))
            (destination / f"{prefixes[index]}-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[1:3] == ["-m", "venv"]:
            scripts = Path(command[-1]) / "Scripts"
            scripts.mkdir(parents=True)
            (scripts / "python.exe").write_bytes(b"python")
            (Path(command[-1]) / "Lib" / "site-packages").mkdir(parents=True)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[1:4] == ["-m", "pip", "install"]:
            scripts = Path(command[0]).parent
            for name in ("pte.exe", "czsc-trader.exe", "pte-watchdog.exe"):
                (scripts / name).write_bytes(b"launcher")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[1] == "-c":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(command)

    with pytest.raises(RuntimeError, match="service host already exists"):
        assemble_release(
            repo_root=repo,
            runtime_root=runtime,
            release_id="v0.4.1",
            source_python=Path("C:/Python/python.exe"),
            runner=run,
        )
    assert not (runtime / "releases" / "v0.4.1").exists()
