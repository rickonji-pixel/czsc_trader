"""Operator CLI for read-only Longbridge observation and shadow imports."""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
import signal
from threading import Event
from typing import Sequence

from .live_engine import LongbridgeLiveEngine
from .live_store import LiveTradeStore
from .longbridge_readonly import LongbridgeReadOnlyGateway
from .qualification import require_live_ready, strategy_identity
from .shadow import import_pte_shadow
from .store import LiveObservationStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="longbridge-live")
    actions = parser.add_subparsers(dest="action", required=True)

    for name in ("preflight", "snapshot"):
        leaf = actions.add_parser(name)
        leaf.add_argument("--env-file", type=Path, required=True)
        leaf.add_argument("--symbol", default="MU.US")
        if name == "snapshot":
            leaf.add_argument("--database", type=Path, required=True)
            leaf.add_argument("--history-days", type=int, default=30)

    shadow = actions.add_parser("shadow-sync")
    shadow.add_argument("--database", type=Path, required=True)
    shadow.add_argument("--source-database", type=Path, required=True)
    shadow.add_argument("--source-account-id", required=True)
    shadow.add_argument("--symbol", default="MU.US")

    status = actions.add_parser("status")
    status.add_argument("--database", type=Path, required=True)
    status.add_argument("--account-id")

    register = actions.add_parser("register")
    register.add_argument("--repo-root", type=Path, required=True)
    register.add_argument("--database", type=Path, required=True)
    register.add_argument("--account-id", required=True)
    register.add_argument("--strategy", required=True)
    register.add_argument("--strategy-version", required=True)
    register.add_argument("--symbol", default="MU.US")
    register.add_argument("--max-order-notional", type=Decimal, default=Decimal("1"))
    register.add_argument("--max-gross-notional", type=Decimal, default=Decimal("1"))
    register.add_argument("--cash-reserve", type=Decimal, default=Decimal("0"))

    limits = actions.add_parser("risk-limits")
    limits.add_argument("--database", type=Path, required=True)
    limits.add_argument("--account-id", required=True)
    limits.add_argument("--max-order-notional", type=Decimal, required=True)
    limits.add_argument("--max-gross-notional", type=Decimal, required=True)
    limits.add_argument("--cash-reserve", type=Decimal, required=True)
    limits.add_argument("--confirm", required=True)

    challenge = actions.add_parser("arm-challenge")
    challenge.add_argument("--database", type=Path, required=True)
    challenge.add_argument("--account-id", required=True)

    arm = actions.add_parser("arm")
    arm.add_argument("--repo-root", type=Path, required=True)
    arm.add_argument("--database", type=Path, required=True)
    arm.add_argument("--account-id", required=True)
    arm.add_argument("--challenge-id", required=True)
    arm.add_argument("--confirm", required=True)
    arm.add_argument("--hours", type=int, default=8)
    arm.add_argument("--actor", required=True)
    arm.add_argument("--reason", required=True)

    disarm = actions.add_parser("disarm")
    disarm.add_argument("--database", type=Path, required=True)
    disarm.add_argument("--account-id", required=True)
    disarm.add_argument("--reason", required=True)

    for name in ("startup-reconcile", "plan", "cycle", "serve", "cancel"):
        leaf = actions.add_parser(name)
        leaf.add_argument("--repo-root", type=Path, required=True)
        leaf.add_argument("--data-dir", type=Path, required=True)
        leaf.add_argument("--database", type=Path, required=True)
        leaf.add_argument("--env-file", type=Path, required=True)
        leaf.add_argument("--account-id", required=True)
        if name == "serve":
            leaf.add_argument("--interval", type=float, default=15.0)
        elif name == "cancel":
            leaf.add_argument("--order-id", required=True)
            leaf.add_argument("--confirm", required=True)
    return parser


def _emit(result: object, *, status: str = "PASS") -> None:
    print(json.dumps({"status": status, "result": result}, ensure_ascii=False, default=str))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "preflight":
            gateway = LongbridgeReadOnlyGateway(symbol=args.symbol, env_file=args.env_file)
            snapshot = gateway.snapshot()
            _emit(
                {
                    "capabilities": gateway.capability_report(),
                    "balance_records": len(snapshot["balances"]),
                    "position_records": len(snapshot["positions"]),
                    "today_order_records": len(snapshot["today_orders"]),
                    "today_execution_records": len(snapshot["today_executions"]),
                }
            )
            return 0
        if args.action == "snapshot":
            gateway = LongbridgeReadOnlyGateway(symbol=args.symbol, env_file=args.env_file)
            store = LiveObservationStore(args.database)
            try:
                current = store.save_snapshot(gateway.snapshot())
                history = store.save_snapshot(gateway.history(days=args.history_days))
                result = {
                    "capabilities": gateway.capability_report(),
                    "current": current,
                    "history": history,
                    "store": store.status(),
                }
            finally:
                store.close()
            _emit(result)
            return 0
        if args.action == "shadow-sync":
            store = LiveObservationStore(args.database)
            try:
                result = import_pte_shadow(
                    store,
                    source_database=args.source_database,
                    source_account_id=args.source_account_id,
                    symbol=args.symbol,
                )
                result["store"] = store.status()
            finally:
                store.close()
            _emit(result)
            return 0
        if args.action == "status":
            store = LiveObservationStore(args.database)
            try:
                observation = store.status()
            finally:
                store.close()
            result: dict[str, object] = {"broker_observation": observation}
            if args.account_id:
                trade_store = LiveTradeStore(args.database)
                try:
                    result["live_deployment"] = trade_store.status(args.account_id)
                finally:
                    trade_store.close()
                result["live_order_adapter_available"] = True
            _emit(result)
            return 0
        if args.action == "register":
            identity = strategy_identity(
                args.repo_root, args.strategy, args.strategy_version,
            )
            store = LiveTradeStore(args.database)
            try:
                result = store.register_deployment(
                    account_id=args.account_id,
                    strategy_id=args.strategy,
                    strategy_version=args.strategy_version,
                    release_hash=identity["release_hash"],
                    symbol=args.symbol,
                    max_order_notional=args.max_order_notional,
                    max_gross_notional=args.max_gross_notional,
                    cash_reserve=args.cash_reserve,
                )
                result["current_qualification"] = identity["qualification"]
                result["live_trading_enabled"] = False
            finally:
                store.close()
            _emit(result)
            return 0
        if args.action == "risk-limits":
            if args.confirm != "CHANGE_LONG_BRIDGE_REAL_MONEY_LIMITS":
                raise PermissionError("real-money risk-limit confirmation differs")
            store = LiveTradeStore(args.database)
            try:
                result = store.update_risk_limits(
                    args.account_id,
                    max_order_notional=args.max_order_notional,
                    max_gross_notional=args.max_gross_notional,
                    cash_reserve=args.cash_reserve,
                )
            finally:
                store.close()
            _emit(result)
            return 0
        if args.action == "arm-challenge":
            store = LiveTradeStore(args.database)
            try:
                result = store.issue_arm_challenge(args.account_id)
            finally:
                store.close()
            _emit(result)
            return 0
        if args.action == "arm":
            store = LiveTradeStore(args.database)
            try:
                deployment = store.deployment(args.account_id)
                identity = strategy_identity(
                    args.repo_root,
                    deployment["strategy_id"],
                    deployment["strategy_version"],
                )
                if identity["release_hash"] != deployment["release_hash"]:
                    raise PermissionError("live deployment release hash differs")
                require_live_ready(identity)
                result = store.arm(
                    args.account_id,
                    challenge_id=args.challenge_id,
                    confirmation_phrase=args.confirm,
                    hours=args.hours,
                    actor=args.actor,
                    reason=args.reason,
                )
            finally:
                store.close()
            _emit(result)
            return 0
        if args.action == "disarm":
            store = LiveTradeStore(args.database)
            try:
                result = store.disarm(args.account_id, reason=args.reason)
            finally:
                store.close()
            _emit(result)
            return 0
        if args.action in {"startup-reconcile", "plan", "cycle", "serve", "cancel"}:
            engine = LongbridgeLiveEngine(
                repo_root=args.repo_root,
                data_dir=args.data_dir,
                database=args.database,
                env_file=args.env_file,
                account_id=args.account_id,
            )
            try:
                if args.action == "startup-reconcile":
                    result = engine.startup_reconcile()
                elif args.action == "plan":
                    snapshot = engine.read_gateway.snapshot()
                    engine.observation_store.save_snapshot(snapshot)
                    result = engine.plan(snapshot)
                elif args.action == "cycle":
                    result = engine.cycle()
                elif args.action == "cancel":
                    engine.cancel(args.order_id, confirmation=args.confirm)
                    result = {"order_id": args.order_id, "cancel_requested": True}
                else:
                    stop = Event()
                    signal.signal(signal.SIGTERM, lambda *_: stop.set())
                    signal.signal(signal.SIGINT, lambda *_: stop.set())
                    engine.serve(interval=args.interval, stop=stop)
                    result = {"stopped": True}
            finally:
                engine.close()
            _emit(result)
            return 0
    except Exception as exc:
        print(json.dumps({
            "status": "FAIL",
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }, ensure_ascii=False))
        return 1
    raise AssertionError(args.action)
