from pathlib import Path


def test_service_install_command_contains_recovery_and_auto_start(tmp_path: Path) -> None:
    from paper_trading_engine.windows_service import service_commands

    commands = service_commands(
        python_executable=Path("C:/Python/pythonservice.exe"),
        service_module=Path("D:/repo/windows_service.py"),
    )
    assert commands[0][-2:] == ["--startup", "auto"]
    assert commands[1] == [
        "sc.exe", "failure", "CZSC-PTE-Watchdog", "reset=", "86400",
        "actions=", "restart/5000/restart/30000/restart/60000",
    ]
    assert commands[2] == ["sc.exe", "delete", "CZSC-PaperTrading"]


def test_install_stops_before_mutation_without_administrator(tmp_path: Path, capsys) -> None:
    from paper_trading_engine.windows_service import main

    code = main(
        ["install-config", "--repo-root", str(tmp_path.resolve())],
        admin_check=lambda: False,
    )

    assert code == 5
    assert "管理员" in capsys.readouterr().err
    assert not (tmp_path / "state" / "paper_trading" / "service.json").exists()


def test_prepare_service_host_places_servicemanager_beside_host(tmp_path: Path) -> None:
    from paper_trading_engine.windows_service import prepare_service_host

    source = tmp_path / "site" / "servicemanager.pyd"
    source.parent.mkdir()
    source.write_bytes(b"extension")
    target = tmp_path / "venv"
    prepare_service_host(source, target)

    assert (target / "servicemanager.pyd").read_bytes() == b"extension"


def test_bootstrap_adds_venv_and_repository_packages(tmp_path: Path) -> None:
    from paper_trading_engine.windows_service import build_bootstrap_source

    source = build_bootstrap_source(tmp_path / ".venv", tmp_path)
    assert repr(str(tmp_path / ".venv" / "Lib" / "site-packages")) in source
    assert repr(str(tmp_path / "packages" / "paper_trading_engine" / "src")) in source
    assert "from paper_trading_engine.windows_service import PteWatchdogService" in source


def test_service_config_builds_pte_cli_child_command(tmp_path: Path) -> None:
    from paper_trading_engine.service_config import ServiceConfig

    config = ServiceConfig(repo_root=tmp_path.resolve())
    command = config.pte_command()

    assert command[0] == str(tmp_path.resolve() / ".venv" / "Scripts" / "pte.exe")
    assert command[1:4] == ["serve", "--repo-root", str(tmp_path.resolve())]
    assert command[-4:] == ["--port", "8080", "--data-refresh-time", "19:00"]
    assert config.health_url == "http://127.0.0.1:8080/api/status"
    assert config.watchdog_log_path.name == "watchdog.log"
