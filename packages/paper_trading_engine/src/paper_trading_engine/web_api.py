"""Resource-scoped read model for the PTE operations console."""

from __future__ import annotations

from datetime import datetime, timezone

from .channel_binding import load_channel_binding


SYSTEM_EVENT_TYPES = {
    "DATA_PUBLICATION_FAILED",
    "DATA_PUBLISHED",
    "SCHEDULER_OPERATION_FAILED",
    "SCHEDULER_OPERATION_RECOVERED",
    "SCHEDULER_CYCLE_FAILED",
}


class ResourceNotFound(KeyError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PteWebApi:
    def __init__(self, operations) -> None:
        self.operations = operations
        self.store = operations.store
        self.virtual = operations.virtual
        self.channel = operations.channel

    def system_status(self) -> dict[str, object]:
        channel = self.channel.status()
        failures = channel.get("scheduler_failures", self.store.operation_failures())
        alerts = [item for item in channel.get("alerts", []) if str(item).startswith("DATA_")]
        return {
            "scope": {"system": "pte"},
            "as_of": _now(),
            "runtime": "RUNNING",
            "data_cutoff": (channel.get("last_decision") or {}).get("data_cutoff"),
            "last_publication": self.store.get_setting("last_data_publication"),
            "scheduler_failures": failures,
            "futu_connection": "UNAVAILABLE" if "CHANNEL_UNAVAILABLE" in channel.get("alerts", []) else "CONNECTED",
            "alerts": alerts,
            "events": [
                event for event in self.store.recent_events(200)
                if event["event_type"] in SYSTEM_EVENT_TYPES
            ],
        }

    def virtual_accounts(self) -> dict[str, object]:
        accounts = []
        for row in self.store.virtual_accounts():
            status = self.virtual.status(row["account_id"])
            decision = status.get("last_decision") or {}
            accounts.append({
                "account_id": row["account_id"], "name": row["name"],
                "strategy_id": row["strategy_id"],
                "release_id": f'{row["strategy_id"]}-{row["strategy_version"]}',
                "release_hash": row["release_hash"], "paused": bool(row["paused"]),
                "health": row["health"], "total_assets": row["total_assets"],
                "quantity": row["quantity"], "latest_action": decision.get("action"),
                "alert_count": 1 if row.get("last_error") else 0,
            })
        return {
            "scope": {"resource": "virtual_accounts"}, "as_of": _now(),
            "default_account_id": accounts[0]["account_id"] if accounts else None,
            "accounts": accounts,
        }

    def virtual_account_snapshot(self, account_id: str) -> dict[str, object]:
        try:
            status = self.virtual.status(account_id)
        except KeyError as exc:
            raise ResourceNotFound(account_id) from exc
        strategy_id = status.get("strategy_id")
        version = status.get("strategy_version")
        release_id = f"{strategy_id}-{version}"
        account_keys = {
            "account_id", "name", "strategy_id", "strategy_name_snapshot", "strategy_version",
            "release_hash", "qualification_snapshot", "symbol", "initial_cash", "cash",
            "frozen_cash", "total_assets", "quantity", "average_cost", "realized_pnl",
            "cycle_target", "paused", "observation_start", "last_settlement_session", "health",
            "last_error", "created_at", "updated_at",
        }
        events = [
            event for event in self.store.recent_events(200)
            if event.get("payload", {}).get("account_id") == account_id
        ]
        return {
            "scope": {"account_id": account_id, "strategy_id": strategy_id,
                      "release_id": release_id, "release_hash": status.get("release_hash")},
            "as_of": _now(),
            "account": {key: status.get(key) for key in account_keys},
            "decision": status.get("last_decision"),
            "orders": [row for row in status.get("orders", []) if row.get("account_id") == account_id],
            "fills": [row for row in status.get("fills", []) if row.get("account_id") == account_id],
            "metrics": status.get("metrics", {}),
            "alerts": ([{"code": "ACCOUNT_BLOCKED", "message": status["last_error"]}]
                       if status.get("last_error") else []),
            "events": events,
        }

    def channel_snapshot(self, channel: str) -> dict[str, object]:
        if channel != "futu":
            raise ResourceNotFound(channel)
        status = self.channel.status()
        binding = load_channel_binding(self.store)
        events = [
            event for event in self.store.recent_events(200)
            if event.get("payload", {}).get("channel") == "futu"
            or event["event_type"].startswith(("ORDER_", "FILL_", "CANCEL_", "CHANNEL_"))
        ]
        return {
            "scope": {"channel": "futu", "account_type": "broker_simulation"},
            "as_of": _now(), "binding": None if binding is None else binding.to_dict(),
            "account": status.get("account"), "actual_quantity": status.get("actual_quantity"),
            "decision": status.get("last_decision"), "orders": status.get("orders", []),
            "paused": status.get("paused"), "quote_health": status.get("quote_health"),
            "connection_error": status.get("channel_error"), "alerts": status.get("alerts", []),
            "scheduler_failures": status.get("scheduler_failures", []), "events": events,
        }

    def comparison(self, account_ids: list[str]) -> dict[str, object]:
        known = {row["account_id"] for row in self.store.virtual_accounts()}
        selected = list(dict.fromkeys(account_ids)) if account_ids else sorted(known)
        missing = next((item for item in selected if item not in known), None)
        if missing:
            raise ResourceNotFound(missing)
        statuses = [self.virtual.status(account_id) for account_id in selected]
        starts = [row["metrics"].get("observation_start") for row in statuses]
        ends = [row["metrics"].get("observation_end") for row in statuses]
        common_start = max(starts) if statuses and all(starts) else None
        common_end = min(ends) if statuses and all(ends) else None
        valid_window = common_start is not None and common_end is not None and common_start <= common_end
        accounts = []
        for status in statuses:
            metrics = (self.virtual.metrics(status["account_id"], common_start, common_end)
                       if valid_window else status["metrics"])
            accounts.append({"account_id": status["account_id"], "name": status["name"],
                             "release_id": f'{status["strategy_id"]}-{status["strategy_version"]}',
                             "metrics": metrics})
        return {
            "scope": {"resource": "comparison"}, "as_of": _now(),
            "common_window": {"start": common_start, "end": common_end},
            "priority_metrics": ["maximum_drawdown", "calmar_ratio", "win_loss_ratio", "total_return"],
            "accounts": accounts,
        }
