from __future__ import annotations

from pathlib import Path

import pytest

from paper_trading_engine.store import PaperStore


class FakeVirtual:
    def __init__(self, store):
        self.store = store

    def status(self, account_id):
        account = self.store.virtual_account(account_id)
        return {
            **account,
            "last_decision": {"decision_id": f"DEC-{account_id}", "strategy": {
                "strategy_id": account["strategy_id"], "version": account["strategy_version"],
                "release_id": f'{account["strategy_id"]}-{account["strategy_version"]}',
                "release_hash": account["release_hash"],
            }},
            "orders": self.store.virtual_orders(account_id),
            "fills": self.store.virtual_fills(account_id),
            "metrics": {"observation_start": None, "observation_end": None,
                        "total_return": 0.0, "maximum_drawdown": 0.0,
                        "calmar_ratio": None, "win_loss_ratio": None, "closed_trades": 0},
        }

    def metrics(self, account_id, start=None, end=None):
        return self.status(account_id)["metrics"]


class FakeChannel:
    def __init__(self, store):
        self.store = store

    def status(self):
        return {"environment": "SIMULATE", "market": "CN", "symbol": "588080.SH",
                "paused": False, "account": {"cash": 1_000_000}, "orders": [],
                "last_decision": None, "alerts": [], "events": [], "scheduler_failures": []}


class FakeOperations:
    def __init__(self, store):
        self.store = store
        self.virtual = FakeVirtual(store)
        self.channel = FakeChannel(store)

    def status(self):
        return {"environment": "SIMULATE", "symbol": "588080.SH"}


@pytest.fixture
def api(tmp_path: Path):
    from paper_trading_engine.web_api import PteWebApi

    store = PaperStore(tmp_path / "runtime.db")
    for account_id, version, value in (("alpha", "v1", "a"), ("beta", "v2", "b")):
        store.create_virtual_account(
            account_id, account_id.title(), f"legacy-{version}", value * 64, 100000,
            strategy_id="S001", strategy_name_snapshot="策略", strategy_version=version,
            release_hash=value * 64, qualification_snapshot="PAPER_READY",
        )
        store.save_virtual_decision(account_id, {"decision_id": f"DEC-{account_id}"}, None)
    value = PteWebApi(FakeOperations(store))
    yield value
    store.close()


def test_account_snapshot_is_strictly_scoped(api):
    snapshot = api.virtual_account_snapshot("beta")

    assert snapshot["scope"] == {
        "account_id": "beta", "strategy_id": "S001", "release_id": "S001-v2",
        "release_hash": "b" * 64,
    }
    assert snapshot["decision"]["decision_id"] == "DEC-beta"
    assert snapshot["account"]["account_id"] == "beta"


def test_unknown_account_raises_resource_not_found(api):
    from paper_trading_engine.web_api import ResourceNotFound

    with pytest.raises(ResourceNotFound, match="missing"):
        api.virtual_account_snapshot("missing")


def test_channel_snapshot_has_channel_scope_and_binding(api):
    from paper_trading_engine.channel_binding import bind_channel_strategy

    bind_channel_strategy(api.store, {
        "strategy_id": "S001", "version": "v2", "release_id": "S001-v2",
        "release_hash": "b" * 64, "qualification": "PAPER_READY",
    }, "tomxiao", "人工确认", [])

    snapshot = api.channel_snapshot("futu")
    assert snapshot["scope"] == {"channel": "futu", "account_type": "broker_simulation"}
    assert snapshot["binding"]["release_id"] == "S001-v2"


def test_comparison_is_read_only_and_uses_requested_accounts(api):
    result = api.comparison(["beta"])
    assert [row["account_id"] for row in result["accounts"]] == ["beta"]
    assert result["priority_metrics"] == [
        "maximum_drawdown", "calmar_ratio", "win_loss_ratio", "total_return",
    ]
