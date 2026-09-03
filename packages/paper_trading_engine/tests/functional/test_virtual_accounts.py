from decimal import Decimal

from paper_trading_engine.store import PaperStore
from paper_trading_engine.virtual_fill import settle_limit_order


def test_ft_pte01_virtual_accounts_are_isolated_persistent_and_conservative(tmp_path):
    database = tmp_path / "runtime.db"
    store = PaperStore(database)
    for account_id, version, marker in (("s001-v1", "v1", "a"), ("r1102-v1", "v1", "b")):
        store.create_virtual_account(
            account_id, f"{account_id}模拟账户", "legacy", marker * 64, 100_000,
            strategy_id="S001" if account_id.startswith("s001") else "S002",
            strategy_name_snapshot="策略", strategy_version=version,
            release_hash=marker * 64, qualification_snapshot="PAPER_READY",
        )
    store.save_virtual_order(
        "s001-v1", "DEC-BUY", "2026-09-03", "ORDER-1",
        {"side": "BUY", "quantity": 1000, "limit_price": 1.0, "fee_rate": 0.0005},
    )
    store.settle_virtual_order("s001-v1", "ORDER-1", "2026-09-03", Decimal("1"), Decimal("0.0005"))
    store.set_virtual_paused("r1102-v1", True)
    assert len(store.virtual_fills("s001-v1")) == 1
    assert store.virtual_fills("r1102-v1") == []
    assert store.virtual_account("r1102-v1")["paused"] == 1
    store.close()

    reopened = PaperStore(database)
    assert reopened.virtual_account("s001-v1")["quantity"] == 1000
    assert reopened.virtual_account("r1102-v1")["cash"] == "100000.0000"
    reopened.rename_virtual_account("s001-v1", "s001-forward", "S001-v1模拟账户")
    assert reopened.virtual_orders("s001-v1") == []
    assert reopened.virtual_orders("s001-forward")[0]["order_id"] == "ORDER-1"
    assert settle_limit_order("BUY", 1000, Decimal("1"), Decimal("1.1"), Decimal("1.2"), Decimal("1")).status == "TOUCH_UNCERTAIN"
    assert settle_limit_order("BUY", 1000, Decimal("1"), Decimal("1.1"), Decimal("1.2"), Decimal("0.99")).status == "FILLED"
    reopened.close()
