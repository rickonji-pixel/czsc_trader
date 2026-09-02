"""One runtime surface for the Futu channel and internal virtual accounts."""

from datetime import date


class UnavailableChannel:
    """Channel-shaped degraded mode used when the Futu adapter cannot initialize."""

    def __init__(self, store, symbol: str, error: Exception) -> None:
        self.store = store
        self.symbol = symbol
        self.initial_error = str(error)

    def _raise(self, *args, **kwargs):
        raise RuntimeError(self.initial_error)

    refresh = refresh_account = refresh_orders = refresh_decision_if_changed = _raise

    def status(self):
        return {
            "environment": "SIMULATE", "market": "CN", "symbol": self.symbol,
            "quote_health": "UNKNOWN", "paused": self.store.is_paused(), "account": None,
            "actual_quantity": None, "last_decision": None, "orders": self.store.orders(),
            "alerts": ["CHANNEL_UNAVAILABLE"], "events": self.store.recent_events(50),
            "scheduler_failures": self.store.operation_failures(),
        }

    def pause(self):
        self.store.set_paused(True)
        return self.status()

    def resume(self):
        raise RuntimeError("successful channel reconciliation required before resume")

    def issue_cancel_token(self, channel_order_id):
        raise RuntimeError("channel is unavailable")

    def confirm_cancel(self, channel_order_id, token):
        raise RuntimeError("channel is unavailable")

    def close(self):
        self.store.close()


class PteCoordinator:
    def __init__(self, channel, virtual) -> None:
        self.channel = channel
        self.virtual = virtual
        self.store = channel.store
        initial = getattr(channel, "initial_error", None)
        self._channel_errors: dict[str, str] = {} if initial is None else {"initialization": initial}

    def _call(self, name, operation, *args, **kwargs):
        try:
            result = operation(*args, **kwargs)
        except Exception as exc:
            self._channel_errors[name] = str(exc)
            raise
        self._channel_errors.pop(name, None)
        return result

    def refresh(self):
        try:
            channel = self._call("refresh", self.channel.refresh)
        except Exception as exc:
            self.store.add_event("CHANNEL_REFRESH_FAILED", {"error": str(exc)})
            channel = self.channel.status()
        return self._combined(channel)

    def refresh_account(self):
        return self._call("account", self.channel.refresh_account)

    def refresh_orders(self):
        return self._call("orders", self.channel.refresh_orders)

    def refresh_decision_if_changed(self, *, force=False):
        return self._call("decision", self.channel.refresh_decision_if_changed, force=force)

    def refresh_virtual(self, session: date, bar):
        return self.virtual.refresh_all(session, bar)

    def status(self):
        return self._combined(self.channel.status())

    def _combined(self, channel):
        channel = {
            **channel,
            "channel_error": "; ".join(self._channel_errors.values()) or None,
            "channel_errors": dict(self._channel_errors),
        }
        accounts = [self.virtual.status(row["account_id"]) for row in self.store.virtual_accounts()]
        starts = [item["metrics"]["observation_start"] for item in accounts if item["metrics"]["observation_start"]]
        ends = [item["metrics"]["observation_end"] for item in accounts if item["metrics"]["observation_end"]]
        common_start = max(starts) if len(starts) == len(accounts) and accounts else None
        common_end = min(ends) if len(ends) == len(accounts) and accounts else None
        if common_start and common_end and common_start <= common_end:
            for account in accounts:
                account["comparison_metrics"] = self.virtual.metrics(
                    account["account_id"], common_start, common_end
                )
        return {
            # Compatibility aliases keep an already-running pre-v3 watchdog healthy.
            "environment": channel.get("environment"),
            "symbol": channel.get("symbol"),
            "channel": channel,
            "virtual_accounts": accounts,
            "selected_account": accounts[0] if accounts else None,
            "comparison": {
                "common_start": common_start,
                "common_end": common_end,
                "priority_metrics": ["maximum_drawdown", "calmar_ratio", "win_loss_ratio", "total_return"],
                "accounts": [
                    {
                        "account_id": account["account_id"], "name": account["name"],
                        "metrics": account.get("comparison_metrics", account["metrics"]),
                    }
                    for account in accounts
                ],
            },
        }

    def pause(self): return self.channel.pause()
    def resume(self): return self.channel.resume()
    def issue_cancel_token(self, channel_order_id): return self.channel.issue_cancel_token(channel_order_id)
    def confirm_cancel(self, channel_order_id, token): return self.channel.confirm_cancel(channel_order_id, token)
    def pause_virtual(self, account_id): return self.store.set_virtual_paused(account_id, True)
    def resume_virtual(self, account_id): return self.store.set_virtual_paused(account_id, False)
    def close(self): return self.channel.close()
