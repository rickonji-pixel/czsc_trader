"""One runtime surface for the Futu channel and internal virtual accounts."""

from datetime import date


class PteCoordinator:
    def __init__(self, channel, virtual) -> None:
        self.channel = channel
        self.virtual = virtual
        self.store = channel.store
        self._channel_error: str | None = None

    def refresh(self):
        try:
            channel = self.channel.refresh()
        except Exception as exc:
            self._channel_error = str(exc)
            self.store.add_event("CHANNEL_REFRESH_FAILED", {"error": str(exc)})
            channel = self.channel.status()
        else:
            self._channel_error = None
        return self._combined(channel)

    def refresh_account(self):
        return self.channel.refresh_account()

    def refresh_orders(self):
        return self.channel.refresh_orders()

    def refresh_decision_if_changed(self, *, force=False):
        return self.channel.refresh_decision_if_changed(force=force)

    def refresh_virtual(self, session: date, bar):
        return self.virtual.refresh_all(session, bar)

    def status(self):
        return self._combined(self.channel.status())

    def _combined(self, channel):
        channel = {**channel, "channel_error": self._channel_error}
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
            "selected_account": accounts[0]["account_id"] if accounts else None,
            "comparison": {
                "common_start": common_start,
                "common_end": common_end,
                "priority_metrics": ["maximum_drawdown", "calmar_ratio", "win_loss_ratio", "total_return"],
            },
        }

    def pause(self): return self.channel.pause()
    def resume(self): return self.channel.resume()
    def issue_cancel_token(self, channel_order_id): return self.channel.issue_cancel_token(channel_order_id)
    def confirm_cancel(self, channel_order_id, token): return self.channel.confirm_cancel(channel_order_id, token)
    def pause_virtual(self, account_id): return self.store.set_virtual_paused(account_id, True)
    def resume_virtual(self, account_id): return self.store.set_virtual_paused(account_id, False)
    def close(self): return self.channel.close()
