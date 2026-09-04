"""Command-line entry point for one-shot and continuous paper trading."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from decimal import Decimal
import json
from pathlib import Path
import socket
import secrets
import subprocess
import sys
from threading import Event, Thread
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from .audit import AuditRecorder
from .advice_client import CliAdviceClient
from .data_publisher import CliDataPublisher, seed_runtime_data
from .account_engine import AccountEngine
from .account_chart import AccountChartService
from .futu_execution import FutuExecution
from .futu_gateway import FutuGateway
from .store import PaperStore
from .scheduler import RuntimeScheduler
from .web import create_server
from .coordinator import PteCoordinator, UnavailableExecution


class PortUnavailableError(RuntimeError):
    pass


DEFAULT_VIRTUAL_INITIAL_CASH = Decimal("100000.0000")
LEGACY_VIRTUAL_INITIAL_CASH = Decimal("1000000.0000")
CURRENT_STRATEGY_ID = "S001"
CURRENT_STRATEGY_NAME = "综合基线策略"
CURRENT_STRATEGY_VERSION = "v1"
CURRENT_RELEASE_HASH = "ae422915ff736431d70e0381dd6514ee800d861060cc5568712b55c895ddfb62"
CURRENT_SELECTION_DATA_CUTOFF = "2026-08-28"
CURRENT_QUALIFICATION = "PAPER_READY"
CURRENT_LEGACY_BASELINE = "baseline_20260903"
CURRENT_LEGACY_BASELINE_HASH = (
    "a7af8864e469b72a94c59eb2e012af5f9a634203cdf5a0214391dd2909e9e331"
)
DEFAULT_VIRTUAL_ACCOUNT_ID = "s001-v1"
DEFAULT_VIRTUAL_ACCOUNT_NAME = "S001-v1模拟账户"


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
    identity = create.add_mutually_exclusive_group(required=True)
    identity.add_argument("--strategy")
    identity.add_argument("--baseline")
    create.add_argument("--strategy-version")
    create.add_argument("--initial-cash", default="100000")
    performance = actions.add_parser("performance")
    performance_actions = performance.add_subparsers(dest="performance_action", required=True)
    export = performance_actions.add_parser("export")
    _common(export)
    export.add_argument("--account-id", required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--recorded-by", required=True)
    export.add_argument("--start")
    export.add_argument("--end")
    control = actions.add_parser("control")
    control_actions = control.add_subparsers(dest="control_action", required=True)
    restart = control_actions.add_parser("restart")
    _common(restart)
    restart.add_argument("--host", default="127.0.0.1")
    restart.add_argument("--port", default=8080, type=int)
    restart.add_argument("--wait", default=30.0, type=float)
    return parser


def _default_executable(repo_root: Path) -> Path:
    name = "czsc-trader.exe" if sys.platform == "win32" else "czsc-trader"
    scripts = "Scripts" if sys.platform == "win32" else "bin"
    return repo_root / ".venv" / scripts / name


def build_engine(args: argparse.Namespace):
    seed_runtime_data(args.repo_root / "data" / "raw", args.data_dir, args.symbol)
    store = PaperStore(args.database)
    audit = AuditRecorder(store)
    advice = CliAdviceClient(
        executable=args.advice_executable or _default_executable(args.repo_root),
        repo_root=args.repo_root,
        data_dir=args.data_dir,
        symbol=args.symbol,
        asset=args.asset,
        audit=audit,
    )
    try:
        gateway = FutuGateway(
            symbol=args.symbol, host=args.opend_host, port=args.opend_port, audit=audit
        )
    except Exception as exc:
        audit.record(
            "DEPENDENCY_DEGRADED", source="cli", outcome="FAILURE",
            actor_type="EXTERNAL", actor_id="futu", channel="futu",
            details={"service": "futu", "operation": "initialize", "error": str(exc)},
        )
        execution = UnavailableExecution(store, args.symbol, exc)
    else:
        execution = FutuExecution(store, gateway, symbol=args.symbol, audit=audit)
    try:
        store.virtual_account("baseline-143")
    except KeyError:
        pass
    else:
        store.rename_virtual_account(
            "baseline-143", DEFAULT_VIRTUAL_ACCOUNT_ID, DEFAULT_VIRTUAL_ACCOUNT_NAME,
        )
    try:
        account = store.virtual_account(DEFAULT_VIRTUAL_ACCOUNT_ID)
    except KeyError:
        account = store.create_virtual_account(
            DEFAULT_VIRTUAL_ACCOUNT_ID, DEFAULT_VIRTUAL_ACCOUNT_NAME, CURRENT_LEGACY_BASELINE,
            CURRENT_LEGACY_BASELINE_HASH, DEFAULT_VIRTUAL_INITIAL_CASH,
            strategy_id=CURRENT_STRATEGY_ID,
            strategy_name_snapshot=CURRENT_STRATEGY_NAME,
            strategy_version=CURRENT_STRATEGY_VERSION,
            release_hash=CURRENT_RELEASE_HASH,
            qualification_snapshot=CURRENT_QUALIFICATION,
            selection_data_cutoff=CURRENT_SELECTION_DATA_CUTOFF,
        )
    else:
        if Decimal(account["initial_cash"]) == LEGACY_VIRTUAL_INITIAL_CASH:
            account = store.migrate_pristine_virtual_account_capital(
                DEFAULT_VIRTUAL_ACCOUNT_ID,
                expected_initial_cash=LEGACY_VIRTUAL_INITIAL_CASH,
                new_initial_cash=DEFAULT_VIRTUAL_INITIAL_CASH,
            )
        if account.get("selection_data_cutoff") is None:
            store.backfill_account_selection_cutoff(
                DEFAULT_VIRTUAL_ACCOUNT_ID,
                CURRENT_RELEASE_HASH,
                CURRENT_SELECTION_DATA_CUTOFF,
            )
            account = store.virtual_account(DEFAULT_VIRTUAL_ACCOUNT_ID)
        expected = (
            DEFAULT_VIRTUAL_ACCOUNT_NAME, CURRENT_LEGACY_BASELINE, CURRENT_LEGACY_BASELINE_HASH,
            CURRENT_STRATEGY_ID, CURRENT_STRATEGY_NAME, CURRENT_STRATEGY_VERSION,
            CURRENT_RELEASE_HASH, CURRENT_QUALIFICATION,
            args.symbol.upper(), str(DEFAULT_VIRTUAL_INITIAL_CASH),
            CURRENT_SELECTION_DATA_CUTOFF,
        )
        actual = (
            account["name"], account["baseline_version"], account["baseline_sha256"],
            account["strategy_id"], account["strategy_name_snapshot"],
            account["strategy_version"], account["release_hash"],
            account["qualification_snapshot"],
            account["symbol"], account["initial_cash"], account["selection_data_cutoff"],
        )
        if actual != expected:
            raise ValueError(f"{DEFAULT_VIRTUAL_ACCOUNT_ID} virtual account has a different immutable identity")
    try:
        store.rename_virtual_account("s001-v2", "s001-v2", "S001-v2模拟账户")
    except KeyError:
        pass
    _backfill_selection_cutoffs(
        store,
        audit,
        args.advice_executable or _default_executable(args.repo_root),
        args.repo_root,
    )
    account_chart = AccountChartService(
        store,
        data_dir=args.data_dir,
        cache_dir=args.database.parent / "charts",
        trader_executable=args.advice_executable or _default_executable(args.repo_root),
        audit=audit,
    )
    return PteCoordinator(
        AccountEngine(store, advice, audit=audit),
        execution,
        audit=audit,
        account_chart=account_chart,
    )


def build_publisher(
    args: argparse.Namespace, audit: AuditRecorder | None = None,
) -> CliDataPublisher:
    return CliDataPublisher(
        executable=args.advice_executable or _default_executable(args.repo_root),
        repo_root=args.repo_root,
        data_dir=args.data_dir,
        symbol=args.symbol,
        asset=args.asset,
        start_date=args.data_start,
        audit=audit,
    )


def _strategy_show(
    executable: Path, repo_root: Path, reference: str, version: str | None = None,
) -> dict[str, object]:
    command = [
        str(executable), "strategy", "show", "--repo-root", str(repo_root),
        "--strategy", reference,
    ]
    if version:
        command.extend(["--version", version])
    completed = subprocess.run(
        command,
        check=False, capture_output=True, text=True, encoding="utf-8",
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(completed.stderr.strip() or "strategy validation returned invalid JSON") from exc
    if completed.returncode or payload.get("status") != "PASS":
        error = payload.get("error", {})
        raise RuntimeError(error.get("message") or "strategy validation failed")
    qualification = payload["result"].get("qualification")
    if qualification not in {"PAPER_READY", "LIVE_READY"}:
        raise RuntimeError(f"strategy qualification cannot enter paper trading: {qualification}")
    if not payload["result"].get("selection_data_cutoff"):
        raise RuntimeError("strategy release has no selection_data_cutoff")
    return payload["result"]


def _validate_strategy(args: argparse.Namespace) -> dict[str, object]:
    return _strategy_show(
        args.advice_executable or _default_executable(args.repo_root),
        args.repo_root,
        getattr(args, "strategy", None) or getattr(args, "baseline", None),
        args.strategy_version,
    )


def _backfill_selection_cutoffs(
    store: PaperStore, audit: AuditRecorder, executable: Path, repo_root: Path,
) -> None:
    for account in store.virtual_accounts():
        if account.get("selection_data_cutoff"):
            continue
        try:
            identity = _strategy_show(
                executable,
                repo_root,
                str(account["strategy_id"]),
                str(account["strategy_version"]),
            )
            if identity["release_hash"] != account["release_hash"]:
                raise RuntimeError("stored release hash does not match strategy registry")
            if not store.backfill_account_selection_cutoff(
                account["account_id"],
                account["release_hash"],
                identity["selection_data_cutoff"],
            ):
                raise RuntimeError("selection cutoff backfill was not applied")
            store.set_setting(f"selection_cutoff_error:{account['account_id']}", "")
        except Exception as exc:
            fingerprint = str(exc)
            key = f"selection_cutoff_error:{account['account_id']}"
            if store.get_setting(key) == fingerprint:
                continue
            audit.record(
                "ACCOUNT_CHART_GENERATION_FAILED",
                source="cli",
                outcome="FAILURE",
                actor_type="ENGINE",
                account_id=account["account_id"],
                strategy_id=account.get("strategy_id"),
                strategy_version=account.get("strategy_version"),
                release_hash=account.get("release_hash"),
                symbol=account.get("symbol"),
                details={"operation": "selection_cutoff_backfill", "error": fingerprint},
            )
            store.set_setting(key, fingerprint)


def _read_json(url: str, *, request: Request | None = None, timeout: float = 3.0):
    with urlopen(request or url, timeout=timeout) as response:  # noqa: S310 - localhost only
        return response.status, json.loads(response.read().decode("utf-8"))


def _ensure_control_token(store: PaperStore) -> str:
    token = store.get_setting("control_token")
    if token:
        return token
    token = secrets.token_urlsafe(32)
    store.set_setting("control_token", token)
    return token


def _record_service_lifecycle(
    audit: AuditRecorder, event_type: str, instance_id: str, **details: object,
) -> None:
    audit.record(
        event_type, source="cli", actor_type="ENGINE",
        actor_id=instance_id, details=details,
    )


def _restart_running_pte(args: argparse.Namespace) -> dict[str, object]:
    if args.host != "127.0.0.1":
        raise ValueError("PTE control host must be 127.0.0.1")
    store = PaperStore(args.database)
    try:
        token = store.get_setting("control_token")
    finally:
        store.close()
    if not token:
        raise RuntimeError("PTE control token is unavailable; perform one bootstrap service restart")
    base = f"http://{args.host}:{args.port}"
    _, current = _read_json(base + "/api/system/status")
    old_instance = current.get("instance_id")
    if not old_instance:
        raise RuntimeError("running PTE does not support graceful restart; perform one bootstrap restart")
    request = Request(
        base + "/api/system/restart", data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "X-PTE-Control-Token": token},
    )
    status, accepted = _read_json(request.full_url, request=request)
    if status != 202 or accepted.get("instance_id") != old_instance:
        raise RuntimeError("PTE restart request was not accepted")
    deadline = time.monotonic() + max(1.0, args.wait)
    while time.monotonic() < deadline:
        time.sleep(0.25)
        try:
            _, latest = _read_json(base + "/api/system/status", timeout=1.0)
        except (OSError, URLError, ValueError, json.JSONDecodeError):
            continue
        new_instance = latest.get("instance_id")
        if new_instance and new_instance != old_instance:
            return {
                "status": "READY", "old_instance_id": old_instance,
                "new_instance_id": new_instance,
            }
    raise RuntimeError(f"PTE did not become healthy within {args.wait:g} seconds")


def _run_account_command(args: argparse.Namespace) -> dict[str, object] | list[dict[str, object]]:
    store = PaperStore(args.database)
    try:
        if args.account_action == "list":
            return store.virtual_accounts()
        if args.account_action == "pause":
            return store.set_virtual_paused(args.account_id, True)
        if args.account_action == "resume":
            return store.set_virtual_paused(args.account_id, False)
        identity = _validate_strategy(args)
        legacy = identity["strategy_payload"].get("legacy_identity", {})
        baseline_version = legacy.get("version", identity["release_id"])
        baseline_hash = legacy.get("sha256", identity["release_hash"])
        existing = None
        try:
            existing = store.virtual_account(args.account_id)
        except KeyError:
            pass
        if existing is not None:
            if (
                existing["strategy_id"], existing["strategy_version"],
                existing["release_hash"], existing["name"],
                existing["symbol"], existing["initial_cash"],
                existing["selection_data_cutoff"],
            ) != (
                identity["strategy_id"], identity["version"], identity["release_hash"],
                args.name, args.symbol.upper(),
                str(Decimal(args.initial_cash).quantize(Decimal("0.0001"))),
                identity["selection_data_cutoff"],
            ):
                raise ValueError("account id already exists with a different immutable identity")
            return existing
        return store.create_virtual_account(
            args.account_id, args.name, baseline_version, baseline_hash, args.initial_cash,
            strategy_id=identity["strategy_id"],
            strategy_name_snapshot=identity["name"],
            strategy_version=identity["version"],
            release_hash=identity["release_hash"],
            qualification_snapshot=identity["qualification"],
            selection_data_cutoff=identity["selection_data_cutoff"],
            symbol=args.symbol,
        )
    finally:
        store.close()


def _write(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    engine_factory: Callable[[argparse.Namespace], object] = build_engine,
) -> int:
    args = build_parser().parse_args(argv)
    engine = None
    try:
        if args.action == "performance":
            from .performance_export import export_performance

            store = PaperStore(args.database)
            try:
                result = export_performance(
                    store,
                    args.account_id,
                    args.output,
                    recorded_by=args.recorded_by,
                    start=args.start,
                    end=args.end,
                )
            finally:
                store.close()
            _write({"status": "PASS", "command": "pte.performance.export", "result": result})
            return 0
        if args.action == "account":
            result = _run_account_command(args)
            _write({"status": "PASS", "command": f"pte.account.{args.account_action}", "result": result})
            return 0
        if args.action == "control":
            result = _restart_running_pte(args)
            _write({"status": "PASS", "command": "pte.control.restart", "result": result})
            return 0
        if args.action == "serve":
            probe_port(args.host, args.port)
        engine = engine_factory(args)
        if args.action == "once":
            result = engine.refresh()
            _write({"status": "PASS", "command": "pte.once", "result": result})
            return 0
        stopped = Event()
        control_token = _ensure_control_token(engine.store)
        audit = AuditRecorder(engine.store)
        instance_id = uuid4().hex
        server_holder = {}

        def graceful_restart():
            engine.begin_shutdown()
            stopped.set()
            server_holder["server"].shutdown()

        engine.startup()
        server = create_server(
            engine, host=args.host, port=args.port, control_token=control_token,
            restart_callback=graceful_restart, instance_id=instance_id,
        )
        server_holder["server"] = server
        scheduler = RuntimeScheduler(
            engine,
            build_publisher(args, audit),
            engine.store,
            order_interval=args.order_interval,
            account_interval=args.account_interval,
            decision_interval=args.decision_interval,
            publish_time=args.data_refresh_time,
            audit=audit,
        )
        worker = Thread(target=scheduler.run, args=(stopped,), name="pte-scheduler", daemon=True)
        worker.start()
        _record_service_lifecycle(
            audit, "SERVICE_STARTED", instance_id,
            host=args.host, port=server.server_port,
        )
        sys.stderr.write(f"PTE listening on http://{args.host}:{server.server_port}\n")
        try:
            server.serve_forever()
        finally:
            stopped.set()
            worker.join(timeout=30.0)
            server.server_close()
            _record_service_lifecycle(audit, "SERVICE_STOPPED", instance_id)
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
