class Store:
    def __init__(self): self.events = []
    def add_event(self, kind, payload): self.events.append((kind, payload))
    def virtual_accounts(self): return [{"account_id": "baseline-143"}]


class BrokenChannel:
    def __init__(self):
        self.store = Store()
        self.draining = False
    def refresh(self): raise RuntimeError("OpenD unavailable")
    def status(self):
        return {"environment": "SIMULATE", "symbol": "588080.SH", "quote_health": "UNKNOWN"}
    def close(self): pass
    def refresh_account(self): raise RuntimeError("account unavailable")
    def refresh_orders(self): return self.status()
    def begin_shutdown(self): self.draining = True


class Virtual:
    def __init__(self): self.draining = False
    def status(self, account_id):
        return {"account_id": account_id, "name": "候选143", "metrics": {"observation_start": None, "observation_end": None}}
    def begin_shutdown(self): self.draining = True


def test_channel_failure_keeps_combined_virtual_status_and_watchdog_aliases():
    from paper_trading_engine.coordinator import PteCoordinator

    channel = BrokenChannel()
    result = PteCoordinator(channel, Virtual()).refresh()

    assert result["environment"] == "SIMULATE"
    assert result["symbol"] == "588080.SH"
    assert result["channel"]["channel_error"] == "OpenD unavailable"
    assert result["virtual_accounts"][0]["account_id"] == "baseline-143"
    assert result["default_account_id"] == "baseline-143"
    assert "selected_account" not in result
    assert result["comparison"]["accounts"][0]["account_id"] == "baseline-143"
    assert channel.store.events[0][0] == "CHANNEL_REFRESH_FAILED"


def test_channel_operation_error_remains_visible_while_virtual_status_works():
    from paper_trading_engine.coordinator import PteCoordinator

    coordinator = PteCoordinator(BrokenChannel(), Virtual())
    try:
        coordinator.refresh_account()
    except RuntimeError:
        pass
    else:
        raise AssertionError("channel operation must report its failure to scheduler")

    result = coordinator.status()
    assert result["channel"]["channel_errors"] == {"account": "account unavailable"}
    assert result["virtual_accounts"][0]["account_id"] == "baseline-143"


def test_begin_shutdown_drains_both_execution_surfaces():
    from paper_trading_engine.coordinator import PteCoordinator

    channel, virtual = BrokenChannel(), Virtual()
    coordinator = PteCoordinator(channel, virtual)
    coordinator.begin_shutdown()

    assert channel.draining is True
    assert virtual.draining is True
    assert channel.store.events[-1][0] == "PTE_RESTART_REQUESTED"
