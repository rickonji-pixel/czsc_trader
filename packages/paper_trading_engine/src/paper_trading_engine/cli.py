"""Command-line entry point for one-shot and continuous paper trading."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import json
from pathlib import Path
import sys
from threading import Event, Thread

from .advice_client import CliAdviceClient
from .data_publisher import CliDataPublisher, seed_runtime_data
from .engine import PaperTradingEngine
from .futu_gateway import FutuGateway
from .store import PaperStore
from .scheduler import RuntimeScheduler
from .web import create_server


class PteParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        result = super().parse_args(args, namespace)
        result.repo_root = result.repo_root.resolve()
        if result.database is None:
            result.database = result.repo_root / "state" / "paper_trading" / "runtime.db"
        if result.data_dir is None:
            result.data_dir = result.repo_root / "state" / "paper_trading" / "data"
        return result


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--position-size", required=True, type=int)
    parser.add_argument("--symbol", default="588080.SH")
    parser.add_argument("--asset", choices=("etf", "stock"), default="etf")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--data-dir", type=Path)
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
    serve.add_argument("--order-interval", default=5.0, type=float)
    serve.add_argument("--account-interval", default=60.0, type=float)
    serve.add_argument("--decision-interval", default=5.0, type=float)
    serve.add_argument("--data-refresh-time", default="16:15")
    serve.add_argument("--data-start", default="2020-01-01")
    return parser


def _default_executable(repo_root: Path) -> Path:
    name = "czsc-trader.exe" if sys.platform == "win32" else "czsc-trader"
    scripts = "Scripts" if sys.platform == "win32" else "bin"
    return repo_root / ".venv" / scripts / name


def build_engine(args: argparse.Namespace) -> PaperTradingEngine:
    seed_runtime_data(args.repo_root / "data" / "raw", args.data_dir, args.symbol)
    store = PaperStore(args.database)
    gateway = FutuGateway(symbol=args.symbol, host=args.opend_host, port=args.opend_port)
    advice = CliAdviceClient(
        executable=args.advice_executable or _default_executable(args.repo_root),
        repo_root=args.repo_root,
        data_dir=args.data_dir,
        symbol=args.symbol,
        asset=args.asset,
        position_size=args.position_size,
    )
    return PaperTradingEngine(store, gateway, advice, symbol=args.symbol)


def build_publisher(args: argparse.Namespace) -> CliDataPublisher:
    return CliDataPublisher(
        executable=args.advice_executable or _default_executable(args.repo_root),
        repo_root=args.repo_root,
        data_dir=args.data_dir,
        symbol=args.symbol,
        asset=args.asset,
        start_date=args.data_start,
    )


def _write(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    engine_factory: Callable[[argparse.Namespace], PaperTradingEngine] = build_engine,
) -> int:
    args = build_parser().parse_args(argv)
    engine = None
    try:
        engine = engine_factory(args)
        if args.action == "once":
            result = engine.refresh()
            _write({"status": "PASS", "command": "pte.once", "result": result})
            return 0
        engine.refresh()
        server = create_server(engine, host=args.host, port=args.port)
        stopped = Event()
        scheduler = RuntimeScheduler(
            engine,
            build_publisher(args),
            engine.store,
            order_interval=args.order_interval,
            account_interval=args.account_interval,
            decision_interval=args.decision_interval,
            publish_time=args.data_refresh_time,
        )
        worker = Thread(target=scheduler.run, args=(stopped,), name="pte-scheduler", daemon=True)
        worker.start()
        sys.stderr.write(f"PTE listening on http://{args.host}:{server.server_port}\n")
        try:
            server.serve_forever()
        finally:
            stopped.set()
            server.server_close()
            worker.join(timeout=max(1.0, args.order_interval + 1.0))
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
    finally:
        if engine is not None:
            engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
