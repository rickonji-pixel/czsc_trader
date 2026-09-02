from pathlib import Path


def test_service_install_command_contains_recovery_and_auto_start(tmp_path: Path) -> None:
    from paper_trading_engine.windows_service import service_commands

    commands = service_commands(
        python_executable=Path("C:/Python/pythonservice.exe"),
        service_module=Path("D:/repo/windows_service.py"),
    )
    assert commands[0][-2:] == ["--startup", "auto"]
    assert commands[1] == [
        "sc.exe", "failure", "CZSC-PaperTrading", "reset=", "86400",
        "actions=", "restart/5000/restart/30000/restart/60000",
    ]


def test_install_stops_before_mutation_without_administrator(tmp_path: Path, capsys) -> None:
    from paper_trading_engine.windows_service import main

    code = main(
        ["install-config", "--repo-root", str(tmp_path.resolve())],
        admin_check=lambda: False,
    )

    assert code == 5
    assert "管理员" in capsys.readouterr().err
    assert not (tmp_path / "state" / "paper_trading" / "service.json").exists()
