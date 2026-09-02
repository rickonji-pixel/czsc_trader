"""Windows SCM adapter for the minimal PTE watchdog."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import ctypes
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Event
import winreg

import servicemanager
import win32event
import win32service
import win32serviceutil

from .service_config import ServiceConfig
from .watchdog import Watchdog


SERVICE_NAME = "CZSC-PTE-Watchdog"
LEGACY_SERVICE_NAME = "CZSC-PaperTrading"
REGISTRY_PATH = rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}\Parameters"


def service_commands(python_executable: Path, service_module: Path) -> list[list[str]]:
    return [
        [str(python_executable), str(service_module), "install", "--startup", "auto"],
        ["sc.exe", "failure", SERVICE_NAME, "reset=", "86400", "actions=",
         "restart/5000/restart/30000/restart/60000"],
        ["sc.exe", "delete", LEGACY_SERVICE_NAME],
    ]


def prepare_service_host(servicemanager_path: Path, host_directory: Path) -> None:
    host_directory.mkdir(parents=True, exist_ok=True)
    destination = host_directory / servicemanager_path.name
    if destination.exists() and destination.stat().st_size == servicemanager_path.stat().st_size:
        return
    shutil.copy2(servicemanager_path, destination)


def build_bootstrap_source(venv_root: Path, repo_root: Path) -> str:
    paths = [
        venv_root / "Lib" / "site-packages",
        venv_root / "Lib" / "site-packages" / "win32",
        venv_root / "Lib" / "site-packages" / "win32" / "lib",
        venv_root / "Lib" / "site-packages" / "pywin32_system32",
        repo_root / "packages" / "paper_trading_engine" / "src",
        repo_root / "packages" / "dataflows" / "src",
        repo_root / "src",
    ]
    additions = "\n".join(f"sys.path.insert(0, {str(path)!r})" for path in paths)
    return (
        "import sys\n" + additions
        + "\nfrom paper_trading_engine.windows_service import PteWatchdogService\n"
    )


def _write_config_path(path: Path) -> None:
    with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, REGISTRY_PATH) as key:
        winreg.SetValueEx(key, "ConfigPath", 0, winreg.REG_SZ, str(path))
    with winreg.CreateKey(
        winreg.HKEY_LOCAL_MACHINE,
        rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}\PythonClass",
    ) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "pte_service_bootstrap.PteWatchdogService")


def _read_config_path() -> Path:
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REGISTRY_PATH) as key:
        return Path(winreg.QueryValueEx(key, "ConfigPath")[0])


class PteWatchdogService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = "CZSC Paper Trading Watchdog"
    _svc_description_ = "Starts and monitors the CZSC paper trading engine"
    _exe_name_ = str(Path(sys.base_prefix) / "pythonservice.exe")

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = Event()
        self.stop_handle = win32event.CreateEvent(None, 0, 0, None)
        self.watchdog = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.stop_event.set()
        if self.watchdog is not None:
            self.watchdog.stop_child()
        win32event.SetEvent(self.stop_handle)

    def SvcDoRun(self):
        config = ServiceConfig.load(_read_config_path())
        config.watchdog_log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            config.watchdog_log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
        logger = logging.getLogger("paper_trading_engine.service")
        try:
            self.watchdog = Watchdog(
                command=config.pte_command(),
                working_directory=config.repo_root,
                health_url=config.health_url,
                log_path=config.log_path,
                sleep=self.stop_event.wait,
                logger=logger,
            )
            logger.info("watchdog service started")
            self.watchdog.run(self.stop_event)
            logger.info("watchdog service stopped")
        except Exception:
            logger.exception("service failed")
            raise


def _is_admin() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def main(
    argv: list[str] | None = None, *, admin_check: Callable[[], bool] = _is_admin
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "install-config":
        if not admin_check():
            sys.stderr.write("安装 CZSC-PTE-Watchdog 需要管理员权限。\n")
            return 5
        parser = argparse.ArgumentParser(prog="pte-watchdog install-config")
        parser.add_argument("--repo-root", required=True, type=Path)
        options = parser.parse_args(arguments[1:])
        repo_root = options.repo_root.resolve()
        prepare_service_host(Path(servicemanager.__file__), Path(sys.exec_prefix))
        prepare_service_host(Path(servicemanager.__file__), Path(sys.base_prefix))
        virtual_host = Path(sys.exec_prefix) / "pythonservice.exe"
        base_host = Path(sys.base_prefix) / "pythonservice.exe"
        if not base_host.exists() or base_host.stat().st_size != virtual_host.stat().st_size:
            shutil.copy2(virtual_host, base_host)
        bootstrap = Path(sys.base_prefix) / "pte_service_bootstrap.py"
        bootstrap.write_text(
            build_bootstrap_source(Path(sys.exec_prefix), repo_root), encoding="utf-8"
        )
        config = ServiceConfig(repo_root=repo_root)
        path = config.repo_root / "state" / "paper_trading" / "service.json"
        config.save(path)
        win32serviceutil.HandleCommandLine(
            PteWatchdogService, argv=[sys.argv[0], "--startup", "auto", "install"]
        )
        _write_config_path(path)
        commands = service_commands(Path(sys.executable), Path(__file__))
        subprocess.run(["sc.exe", "stop", LEGACY_SERVICE_NAME], check=False, capture_output=True)
        subprocess.run(commands[2], check=False, capture_output=True)
        subprocess.run(commands[1], check=True)
        return 0
    win32serviceutil.HandleCommandLine(PteWatchdogService, argv=[sys.argv[0], *arguments])
    return 0


if __name__ == "__main__":
    servicemanager.Initialize()
    raise SystemExit(main())
