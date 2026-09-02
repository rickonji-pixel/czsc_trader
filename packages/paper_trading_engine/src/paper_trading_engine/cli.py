"""Command-line entry point for one-shot and continuous paper trading."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from decimal import Decimal
from datetime import date
import json
from pathlib import Path
import socket
import subprocess
import sys
from threading import Event, Thread

from .advice_client import CliAdviceClient
from .data_publisher import CliDataPublisher, seed_runtime_data
from .engine import PaperTradingEngine
from .futu_gateway import FutuGateway
from .store import PaperStore
from .scheduler import RuntimeScheduler
from .web import create_server
from .virtual_engine import VirtualAccountEngine
from .coordinator import PteCoordinator, UnavailableChannel


class PortUnavailableError(RuntimeError):
    pass


def probe_port(host: str, port: int) -> None:
    candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        candidate.bind((host, int(port)))
    except OSError as exc:
        raise PortUnavailableError(f"{host}:{port} is already in use") from exc
    finally:
        candidate.close()


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
    parser.add_argument("--position-size", type=int, help=argparse.SUPPRESS)
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
    serve.add_argument("--port", default=8080, type=int)
    serve.add_argument("--order-interval", default=5.0, type=float)
    serve.add_argument("--account-interval", default=60.0, type=float)
    serve.add_argument("--decision-interval", default=5.0, type=float)
    serve.add_argument("--data-refresh-time", default="19:00")
    serve.add_argument("--data-start", default="2020-01-01")
    account = actions.add_parser("account")
    account_actions = account.add_subparsers(dest="account_action", required=True)
    for name in ("list", "pause", "resume"):
        leaf = account_actions.add_parser(name)
        _common(leaf)
        if name != "list":
            leaf.add_argument("--account-id", required=True)
    create = account_actions.add_parser("create")
    _common(create)
    create.add_argument("--account-id", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--baseline", required=True)
    create.add_argument("--initial-cash", required=True)
    create.add_argument("--futu-reference", action="store_true")
    return parser


def _default_executable(repo_root: Path) -> Path:
    name = "czsc-trader.exe" if sys.platform == "win32" else "czsc-trader"
    scripts = "Scripts" if sys.platform == "win32" else "bin"
    return repo_root / ".venv" / scripts / name


def build_engine(args: argparse.Namespace):
    seed_runtime_data(args.repo_root / "data" / "raw", args.data_dir, args.symbol)
    store = PaperStore(args.database)
    advice = CliAdviceClient(
        executable=args.advice_executable or _default_executable(args.repo_root),
        repo_root=args.repo_root,
        data_dir=args.data_dir,
        symbol=args.symbol,
        asset=args.asset,
    )
    try:
        gateway = FutuGateway(symbol=args.symbol, host=args.opend_host, port=args.opend_port)
    except Exception as exc:
        store.add_event("CHANNEL_INITIALIZATION_FAILED", {"error": str(exc)})
        channel = UnavailableChannel(store, args.symbol, exc)
    else:
        channel = PaperTradingEngine(store, gateway, advice, symbol=args.symbol)
    try:
        account = store.virtual_account("baseline-143")
    except KeyError:
        store.create_virtual_account(
            "baseline-143", "候选143", "baseline_20260903",
            "a7af8864e469b72a94c59eb2e012af5f9a634203cdf5a0214391dd2909e9e331",
            1_000_000, is_futu_reference=True,
        )
    else:
        expected = (
            "候选143", "baseline_20260903",
            "a7af8864e469b72a94c59eb2e012af5f9a634203cdf5a0214391dd2909e9e331",
            args.symbol.upper(), "1000000.0000",
        )
        actual = (
            account["name"], account["baseline_version"], account["baseline_sha256"],
            account["symbol"], account["initial_cash"],
        )
        if actual != expected:
            raise ValueError("baseline-143 virtual account has a different immutable identity")
        if not any(row["is_futu_reference"] for row in store.virtual_accounts()):
            store.set_futu_reference("baseline-143")
    return PteCoordinator(channel, VirtualAccountEngine(store, advice))


def build_publisher(args: argparse.Namespace) -> CliDataPublisher:
    return CliDataPublisher(
        executable=args.advice_executable or _default_executable(args.repo_root),
        repo_root=args.repo_root,
        data_dir=args.data_dir,
        symbol=args.symbol,
        asset=args.asset,
        start_date=args.data_start,
    )


def _validate_baseline(args: argparse.Namespace) -> dict[str, object]:
    executable = args.advice_executable or _default_executable(args.repo_root)
    completed = subprocess.run(
        [str(executable), "baseline", "validate", "--repo-root", str(args.repo_root),
         "--version", args.baseline, "--symbol", args.symbol],
        check=False, capture_output=True, text=True, encoding="utf-8",
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(completed.stderr.strip() or "baseline validation returned invalid JSON") from exc
    if completed.returncode or payload.get("status") != "PASS":
        error = payload.get("error", {})
        raise RuntimeError(error.get("message") or "baseline validation failed")
    return payload["result"]


def _run_account_command(args: argparse.Namespace) -> dict[str, object] | list[dict[str, object]]:
    store = PaperStore(args.database)
    try:
        if args.account_action == "list":
            return store.virtual_accounts()
        if args.account_action == "pause":
            return store.set_virtual_paused(args.account_id, True)
        if args.account_action == "resume":
            return store.set_virtual_paused(args.account_id, False)
        identity = _validate_baseline(args)
        existing = None
        try:
            existing = store.virtual_account(args.account_id)
        except KeyError:
            pass
        if existing is not None:
            if (
                existing["baseline_version"], existing["baseline_sha256"], existing["name"],
                existing["symbol"], existing["initial_cash"],
            ) != (
                identity["version"], identity["sha256"], args.name, args.symbol.upper(),
                str(Decimal(args.initial_cash).quantize(Decimal("0.0001"))),
            ):
                raise ValueError("account id already exists with a different immutable identity")
            return store.set_futu_reference(args.account_id) if args.futu_reference else existing
        return store.create_virtual_account(
            args.account_id, args.name, identity["version"], identity["sha256"], args.initial_cash,
            symbol=args.symbol, is_futu_reference=args.futu_reference,
        )
    finally:
        store.close()


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
        if args.action == "account":
            result = _run_account_command(args)
            _write({"status": "PASS", "command": f"pte.account.{args.account_action}", "result": result})
            return 0
        if args.action == "serve":
            probe_port(args.host, args.port)
        engine = engine_factory(args)
        virtual = getattr(engine, "virtual", None)
        if virtual is not None:
            try:
                virtual.refresh_from_data(date.today(), args.data_dir, args.symbol)
            except Exception as exc:
                engine.store.add_event("VIRTUAL_STARTUP_FAILED", {"error": str(exc)})
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
            virtual_refresh=lambda session: engine.virtual.refresh_from_data(
                session, args.data_dir, args.symbol
            ),
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
