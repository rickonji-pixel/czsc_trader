"""Resource-scoped read model for the PTE operations console."""

from __future__ import annotations

from datetime import datetime, timezone

from .audit import AuditCategory, AuditOutcome, AuditSeverity, EVENT_CATALOG
from .store import DEFAULT_FUTU_CAPITAL_POOL


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
            "data_cutoff": self.store.get_setting("last_data_publish_date"),
            "last_publication": self.store.get_setting("last_data_publication"),
            "scheduler_failures": failures,
            "futu_connection": "UNAVAILABLE" if "CHANNEL_UNAVAILABLE" in channel.get("alerts", []) else "CONNECTED",
            "alerts": alerts,
            "events": [
                event for event in self.store.recent_events(200)
                if event["category"] == AuditCategory.SYSTEM
            ],
        }

    def audit_events(self, filters: dict[str, str]) -> dict[str, object]:
        allowed = {
            "category", "event_type", "severity", "outcome", "account_id",
            "strategy_id", "channel", "decision_id", "order_id", "correlation_id",
            "before_id", "limit",
        }
        unknown = sorted(set(filters) - allowed)
        if unknown:
            raise ValueError(f"unknown audit filter: {unknown[0]}")
        enum_filters = {
            "category": AuditCategory,
            "severity": AuditSeverity,
            "outcome": AuditOutcome,
        }
        for name, enum_type in enum_filters.items():
            value = filters.get(name)
            if value:
                try:
                    enum_type(value)
                except ValueError as exc:
                    raise ValueError(f"invalid audit {name}: {value}") from exc
        event_type = filters.get("event_type")
        if event_type and event_type not in EVENT_CATALOG:
            raise ValueError(f"invalid audit event_type: {event_type}")
        query = dict(filters)
        for name in ("before_id", "limit"):
            if name in query:
                try:
                    query[name] = int(query[name])
                except ValueError as exc:
                    raise ValueError(f"audit event {name} must be an integer") from exc
        events = self.store.query_audit_events(**query)
        limit = int(query.get("limit", 50))
        return {
            "scope": {"resource": "audit_events", **{
                key: value for key, value in filters.items()
                if key not in {"before_id", "limit"}
            }},
            "as_of": _now(),
            "events": events,
            "next_before_id": events[-1]["id"] if len(events) == limit else None,
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
            "last_error", "channel_id", "status", "created_at", "updated_at",
            "selection_data_cutoff",
        }
        events = self.store.query_audit_events(account_id=account_id, limit=200)
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

    def virtual_account_chart(self, account_id: str) -> dict[str, object]:
        try:
            self.store.virtual_account(account_id)
        except KeyError as exc:
            raise ResourceNotFound(account_id) from exc
        if self.operations.account_chart is None:
            raise ResourceNotFound(account_id)
        return self.operations.account_chart.status(account_id)

    def virtual_account_chart_path(self, account_id: str):
        if self.operations.account_chart is None:
            raise ResourceNotFound(account_id)
        return self.operations.account_chart.chart_path(account_id)

    def channel_snapshot(self, channel: str) -> dict[str, object]:
        if channel != "futu":
            raise ResourceNotFound(channel)
        status = self.channel.status()
        events = self.store.query_audit_events(channel="futu", limit=200)
        accounts = [
            {
                "account_id": row["account_id"], "name": row["name"],
                "release_id": f'{row["strategy_id"]}-{row["strategy_version"]}',
                "initial_cash": row["initial_cash"], "cash": row["cash"],
                "frozen_cash": row["frozen_cash"], "quantity": row["quantity"],
                "paused": bool(row["paused"]), "status": row["status"],
            }
            for row in self.store.virtual_accounts()
            if row.get("channel_id") == "futu"
        ]
        capital_pool = float(
            self.store.get_setting("futu_capital_pool") or DEFAULT_FUTU_CAPITAL_POOL
        )
        allocated = sum(float(row["initial_cash"]) for row in accounts)
        unallocated = capital_pool - allocated
        return {
            "scope": {"channel": "futu", "account_type": "broker_simulation"},
            "as_of": _now(),
            "account": status.get("account"), "actual_quantity": status.get("actual_quantity"),
            "accounts": accounts,
            "capital_pool": capital_pool,
            "allocated_capital": allocated,
            "unallocated_capital": unallocated,
            "orders": status.get("orders", []), "fills": self.store.account_fills(),
            "paused": status.get("paused"),
            "reconciliation_status": status.get("reconciliation_status"),
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
