"""Windows SCM host for the paper-trading runtime."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import ctypes
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
import winreg

import servicemanager
import win32event
import win32service
import win32serviceutil

from .cli import build_engine, build_parser, build_publisher, probe_port
from .scheduler import RuntimeScheduler
from .service_config import ServiceConfig
from .web import create_server


SERVICE_NAME = "CZSC-PaperTrading"
REGISTRY_PATH = rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}\Parameters"


def service_commands(python_executable: Path, service_module: Path) -> list[list[str]]:
    return [
        [str(python_executable), str(service_module), "install", "--startup", "auto"],
        ["sc.exe", "failure", SERVICE_NAME, "reset=", "86400", "actions=",
         "restart/5000/restart/30000/restart/60000"],
    ]


def _write_config_path(path: Path) -> None:
    with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, REGISTRY_PATH) as key:
        winreg.SetValueEx(key, "ConfigPath", 0, winreg.REG_SZ, str(path))


def _read_config_path() -> Path:
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REGISTRY_PATH) as key:
        return Path(winreg.QueryValueEx(key, "ConfigPath")[0])


class PteWindowsService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = "CZSC Paper Trading Engine"
    _svc_description_ = "Broker-neutral simulated trading observation and execution runtime"

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = Event()
        self.stop_handle = win32event.CreateEvent(None, 0, 0, None)
        self.server = None
        self.engine = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.stop_event.set()
        if self.server is not None:
            self.server.shutdown()
        win32event.SetEvent(self.stop_handle)

    def SvcDoRun(self):
        config = ServiceConfig.load(_read_config_path())
        config.log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            config.log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
        logger = logging.getLogger("paper_trading_engine.service")
        try:
            args = build_parser().parse_args(config.serve_arguments())
            probe_port(args.host, args.port)
            self.engine = build_engine(args)
            self.engine.refresh()
            self.server = create_server(self.engine, host=args.host, port=args.port)
            scheduler = RuntimeScheduler(
                self.engine, build_publisher(args), self.engine.store,
                order_interval=args.order_interval, account_interval=args.account_interval,
                decision_interval=args.decision_interval, publish_time=args.data_refresh_time,
            )
            worker = Thread(target=scheduler.run, args=(self.stop_event,), daemon=True)
            worker.start()
            logger.info("service started on %s:%s", args.host, args.port)
            try:
                self.server.serve_forever()
            finally:
                self.stop_event.set()
                self.server.server_close()
                worker.join(timeout=10)
                self.engine.close()
                logger.info("service stopped")
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
            sys.stderr.write("安装 CZSC-PaperTrading 需要管理员权限。\n")
            return 5
        parser = argparse.ArgumentParser(prog="pte-service install-config")
        parser.add_argument("--repo-root", required=True, type=Path)
        options = parser.parse_args(arguments[1:])
        config = ServiceConfig(repo_root=options.repo_root.resolve())
        path = config.repo_root / "state" / "paper_trading" / "service.json"
        config.save(path)
        win32serviceutil.HandleCommandLine(
            PteWindowsService, argv=[sys.argv[0], "--startup", "auto", "install"]
        )
        _write_config_path(path)
        subprocess.run(service_commands(Path(sys.executable), Path(__file__))[1], check=True)
        return 0
    win32serviceutil.HandleCommandLine(PteWindowsService, argv=[sys.argv[0], *arguments])
    return 0


if __name__ == "__main__":
    servicemanager.Initialize()
    raise SystemExit(main())
