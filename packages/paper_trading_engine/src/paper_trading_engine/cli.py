"""Command-line entry point for one-shot and continuous paper trading."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import json
from pathlib import Path
import sys
from threading import Event, Thread

from .advice_client import CliAdviceClient
from .engine import PaperTradingEngine
from .futu_gateway import FutuGateway
from .store import PaperStore
from .web import create_server


class PteParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        result = super().parse_args(args, namespace)
        result.repo_root = result.repo_root.resolve()
        if result.database is None:
            result.database = result.repo_root / "state" / "paper_trading" / "runtime.db"
        return result


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--position-size", required=True, type=int)
    parser.add_argument("--symbol", default="588080.SH")
    parser.add_argument("--asset", choices=("etf", "stock"), default="etf")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--advice-executable", type=Path)
    parser.add_argument("--opend-host", default="127.0.0.1")
    parser.add_argument("--opend-port", default=11111, type=int)


def build_parser() -> argparse.ArgumentParser:
    parser = PteParser(prog="pte")
    actions = parser.add_subparsers(dest="action", required=True)
    once = actions.add_parser("once")
    _common(once)
    serve = actions.add_parser("serve")
    _common(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8765, type=int)
    serve.add_argument("--interval", default=5.0, type=float)
    return parser


def _default_executable(repo_root: Path) -> Path:
    name = "czsc-trader.exe" if sys.platform == "win32" else "czsc-trader"
    scripts = "Scripts" if sys.platform == "win32" else "bin"
    return repo_root / ".venv" / scripts / name


def build_engine(args: argparse.Namespace) -> PaperTradingEngine:
    store = PaperStore(args.database)
    gateway = FutuGateway(symbol=args.symbol, host=args.opend_host, port=args.opend_port)
    advice = CliAdviceClient(
        executable=args.advice_executable or _default_executable(args.repo_root),
        repo_root=args.repo_root,
        symbol=args.symbol,
        asset=args.asset,
        position_size=args.position_size,
    )
    return PaperTradingEngine(store, gateway, advice, symbol=args.symbol)


def _write(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    engine_factory: Callable[[argparse.Namespace], PaperTradingEngine] = build_engine,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        engine = engine_factory(args)
        if args.action == "once":
            result = engine.refresh()
            _write({"status": "PASS", "command": "pte.once", "result": result})
            return 0
        engine.refresh()
        server = create_server(engine, host=args.host, port=args.port)
        stopped = Event()

        def refresh_loop() -> None:
            while not stopped.wait(args.interval):
                try:
                    engine.refresh()
                except Exception as exc:
                    engine.store.add_event("REFRESH_FAILED", {"error": str(exc)})

        worker = Thread(target=refresh_loop, name="pte-refresh", daemon=True)
        worker.start()
        sys.stderr.write(f"PTE listening on http://{args.host}:{server.server_port}\n")
        try:
            server.serve_forever()
        finally:
            stopped.set()
            server.server_close()
            worker.join(timeout=max(1.0, args.interval + 1.0))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        _write(
            {
                "status": "FAIL",
                "command": f"pte.{args.action}",
                "error": {"code": "runtime_error", "message": str(exc)},
            }
        )
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
