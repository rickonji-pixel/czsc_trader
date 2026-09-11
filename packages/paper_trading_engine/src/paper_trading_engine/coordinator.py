"""Account-centric runtime facade for scheduler, CLI, and web console."""

from threading import RLock

from .audit import AuditRecorder
from .futu_gateway import FutuGatewayError


class UnavailableExecution:
    def __init__(self, store, symbol: str, error: Exception) -> None:
        self.store, self.symbol, self.initial_error = store, symbol, str(error)

    def _raise(self, *args, **kwargs):
        raise RuntimeError(self.initial_error)

    refresh = refresh_account = refresh_orders = submit_pending = _raise

    def status(self):
        return {
            "environment": "SIMULATE", "market": "CN", "symbol": self.symbol,
            "account": None, "positions": [], "orders": self.store.account_orders(),
            "paused": self.store.is_paused(),
            "reconciliation_status": "UNAVAILABLE", "alerts": ["CHANNEL_UNAVAILABLE"],
            "scheduler_failures": self.store.operation_failures(),
        }

    def pause(self):
        self.store.set_paused(True)
        return self.status()

    def resume(self): raise RuntimeError("successful channel reconciliation required before resume")
    def issue_cancel_token(self, account_id, channel_order_id): raise RuntimeError("channel is unavailable")
    def confirm_cancel(self, account_id, channel_order_id, token): raise RuntimeError("channel is unavailable")
    def acknowledge_execution_gap(self, account_id, intent_id, resolution_note):
        raise RuntimeError("channel is unavailable")
    def begin_shutdown(self): return None
    def close(self): self.store.close()


class ReconnectableExecution:
    """Keep channel recovery inside PTE when OpenD appears after PTE startup."""

    def __init__(self, store, symbol: str, factory, *, initial=None, error=None) -> None:
        self.store = store
        self.symbol = symbol
        self.factory = factory
        self._delegate = initial
        self._last_error = None if error is None else str(error)
        self._lock = RLock()

    def _execution(self):
        with self._lock:
            if self._delegate is None:
                try:
                    self._delegate = self.factory()
                except Exception as exc:
                    self._last_error = str(exc)
                    raise
                self._last_error = None
            return self._delegate

    def _call(self, method: str, *args, **kwargs):
        delegate = self._execution()
        try:
            return getattr(delegate, method)(*args, **kwargs)
        except FutuGatewayError as exc:
            with self._lock:
                if self._delegate is delegate:
                    close = getattr(getattr(delegate, "broker", None), "close", None)
                    if close is not None:
                        close()
                    self._delegate = None
                    self._last_error = str(exc)
            raise

    def refresh(self): return self._call("refresh")
    def refresh_account(self): return self._call("refresh_account")
    def refresh_orders(self): return self._call("refresh_orders")
    def submit_pending(self, *, reconcile=True):
        return self._call("submit_pending", reconcile=reconcile)

    def status(self):
        if self._delegate is not None:
            return self._delegate.status()
        return {
            "environment": "SIMULATE", "market": "CN", "symbol": self.symbol,
            "account": None, "positions": [], "orders": self.store.account_orders(),
            "paused": self.store.is_paused(),
            "reconciliation_status": "UNAVAILABLE", "alerts": ["CHANNEL_UNAVAILABLE"],
            "scheduler_failures": self.store.operation_failures(),
            "last_error": self._last_error,
        }

    def pause(self):
        self.store.set_paused(True)
        return self.status()

    def resume(self): return self._call("resume")
    def issue_cancel_token(self, account_id, channel_order_id):
        return self._call("issue_cancel_token", account_id, channel_order_id)
    def confirm_cancel(self, account_id, channel_order_id, token):
        return self._call("confirm_cancel", account_id, channel_order_id, token)
    def acknowledge_execution_gap(self, account_id, intent_id, resolution_note):
        return self._call("acknowledge_execution_gap", account_id, intent_id, resolution_note)
    def begin_shutdown(self):
        if self._delegate is not None:
            self._delegate.begin_shutdown()
    def close(self):
        if self._delegate is None:
            self.store.close()
        else:
            self._delegate.close()


class PteCoordinator:
    def __init__(
        self, accounts, execution, audit: AuditRecorder | None = None, account_chart=None,
    ) -> None:
        self.accounts, self.execution = accounts, execution
        self.store = accounts.store
        self.audit = audit or AuditRecorder(self.store)
        self.account_chart = account_chart

    @property
    def virtual(self): return self.accounts

    @property
    def channel(self): return self.execution

    def refresh(self):
        try:
            self.execution.refresh()
        except Exception as exc:
            self.audit.record(
                "DEPENDENCY_DEGRADED", source="coordinator", outcome="FAILURE",
                actor_type="EXTERNAL", actor_id="futu", channel="futu",
                details={"service": "futu", "operation": "refresh", "error": str(exc)},
            )
        self.accounts.refresh_all()
        return self.status()

    def startup(self):
        """Reconcile and resume durable channel work without generating a new decision."""
        try:
            self.execution.refresh()
        except Exception as exc:
            self.audit.record(
                "DEPENDENCY_DEGRADED", source="coordinator", outcome="FAILURE",
                actor_type="EXTERNAL", actor_id="futu", channel="futu",
                details={"service": "futu", "operation": "startup", "error": str(exc)},
            )
        return self.status()

    def refresh_account(self): return self.execution.refresh_account()

    def refresh_orders(self):
        self.execution.refresh_orders()
        return self.execution.submit_pending(reconcile=False)

    def refresh_decisions(self): return self.accounts.refresh_all()

    def refresh_decision(self, account_id: str):
        return self.accounts.refresh_account(account_id)

    def status(self):
        channel = self.execution.status()
        accounts = [self.accounts.status(row["account_id"]) for row in self.store.virtual_accounts()]
        starts = [row["metrics"]["observation_start"] for row in accounts if row["metrics"]["observation_start"]]
        ends = [row["metrics"]["observation_end"] for row in accounts if row["metrics"]["observation_end"]]
        common_start = max(starts) if len(starts) == len(accounts) and accounts else None
        common_end = min(ends) if len(ends) == len(accounts) and accounts else None
        return {
            "environment": channel.get("environment"), "symbol": channel.get("symbol"),
            "channel": channel, "virtual_accounts": accounts,
            "default_account_id": accounts[0]["account_id"] if accounts else None,
            "comparison": {
                "common_start": common_start, "common_end": common_end,
                "priority_metrics": ["maximum_drawdown", "calmar_ratio", "win_loss_ratio", "total_return"],
                "accounts": [
                    {"account_id": row["account_id"], "name": row["name"], "metrics": row["metrics"]}
                    for row in accounts
                ],
            },
        }

    def pause(self): return self.execution.pause()
    def resume(self): return self.execution.resume()
    def issue_cancel_token(self, account_id, channel_order_id):
        return self.execution.issue_cancel_token(account_id, channel_order_id)
    def confirm_cancel(self, account_id, channel_order_id, token):
        return self.execution.confirm_cancel(account_id, channel_order_id, token)
    def acknowledge_execution_gap(self, account_id, intent_id, resolution_note):
        return self.execution.acknowledge_execution_gap(account_id, intent_id, resolution_note)

    def _set_virtual_paused(self, account_id, paused):
        account = self.store.set_virtual_paused(account_id, paused)
        self.audit.record(
            "ACCOUNT_PAUSED" if paused else "ACCOUNT_RESUMED", source="web.control",
            actor_type="OPERATOR", account_id=account_id,
            strategy_id=account.get("strategy_id"), strategy_version=account.get("strategy_version"),
            release_hash=account.get("release_hash"), channel="futu",
        )
        return account

    def pause_virtual(self, account_id): return self._set_virtual_paused(account_id, True)
    def resume_virtual(self, account_id): return self._set_virtual_paused(account_id, False)

    def begin_shutdown(self):
        self.audit.record("RESTART_REQUESTED", source="web.control", actor_type="OPERATOR")
        self.accounts.begin_shutdown()
        self.execution.begin_shutdown()

    def close(self): self.execution.close()
