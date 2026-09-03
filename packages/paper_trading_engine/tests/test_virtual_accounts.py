from decimal import Decimal
from datetime import date
import pytest


STRATEGY = {
    "strategy_id": "S001",
    "name": "综合基线策略",
    "version": "v1",
    "release_id": "S001-v1",
    "release_hash": "a" * 64,
    "qualification": "PAPER_READY",
}


def create_account(store, account_id, name, *, release_hash="a" * 64):
    return store.create_virtual_account(
        account_id,
        name,
        "baseline_20260903",
        "a" * 64,
        Decimal("1000000"),
        strategy_id="S001",
        strategy_name_snapshot="综合基线策略",
        strategy_version="v1",
        release_hash=release_hash,
        qualification_snapshot="PAPER_READY",
    )


def test_virtual_accounts_are_isolated_and_persistent(tmp_path):
    from paper_trading_engine.store import PaperStore

    path = tmp_path / "runtime.db"
    store = PaperStore(path)
    store.create_virtual_account("a", "策略A", "baseline_a", "a" * 64, Decimal("1000000"))
    store.create_virtual_account("b", "策略B", "baseline_b", "b" * 64, Decimal("1000000"))
    store.update_virtual_account("a", cash=Decimal("900000"), quantity=50_000, cycle_target=50_000)
    assert store.virtual_account("a")["quantity"] == 50_000
    assert store.virtual_account("b")["quantity"] == 0
    store.close()
    reopened = PaperStore(path)
    assert reopened.virtual_account("a")["cash"] == "900000.0000"
    assert len(reopened.virtual_accounts()) == 2
    reopened.close()


def test_existing_baseline_143_account_gains_formal_strategy_identity_without_ledger_changes(
    tmp_path,
):
    from paper_trading_engine.store import PaperStore

    path = tmp_path / "runtime.db"
    store = PaperStore(path)
    store.create_virtual_account(
        "baseline-143",
        "候选143",
        "baseline_20260903",
        "a7af8864e469b72a94c59eb2e012af5f9a634203cdf5a0214391dd2909e9e331",
        100_000,
    )
    store.update_virtual_account(
        "baseline-143", cash=90_000, quantity=5_000, cycle_target=5_000
    )
    before = store.virtual_account("baseline-143")
    store.close()

    reopened = PaperStore(path)
    after = reopened.virtual_account("baseline-143")

    assert after["strategy_id"] == "S001"
    assert after["strategy_name_snapshot"] == "综合基线策略"
    assert after["strategy_version"] == "v1"
    assert after["release_hash"] == (
        "ae422915ff736431d70e0381dd6514ee800d861060cc5568712b55c895ddfb62"
    )
    assert after["qualification_snapshot"] == "PAPER_READY"
    for field in ("cash", "quantity", "cycle_target", "created_at", "updated_at"):
        assert after[field] == before[field]
    reopened.close()


def test_virtual_fill_requires_strict_penetration():
    from paper_trading_engine.virtual_fill import settle_limit_order

    equal = settle_limit_order("BUY", 10_000, Decimal("1.500"), Decimal("1.510"), Decimal("1.520"), Decimal("1.500"))
    below = settle_limit_order("BUY", 10_000, Decimal("1.500"), Decimal("1.510"), Decimal("1.520"), Decimal("1.499"))
    improved = settle_limit_order("BUY", 10_000, Decimal("1.500"), Decimal("1.490"), Decimal("1.510"), Decimal("1.480"))
    assert equal.status == "TOUCH_UNCERTAIN"
    assert below.price == Decimal("1.500")
    assert improved.price == Decimal("1.490")


def test_virtual_engine_settles_then_generates_next_decision(tmp_path):
    from paper_trading_engine.contracts import AdviceDecision, OrderSpec
    from paper_trading_engine.store import PaperStore
    from paper_trading_engine.virtual_engine import VirtualAccountEngine

    class Advice:
        def get_decision(
            self, actual_quantity, available_cash, cycle_target_quantity=None,
            strategy_id=None, strategy_version=None,
        ):
            order = OrderSpec("BUY", 10_000, "LIMIT", 1.5, "DAY") if actual_quantity == 0 else None
            return AdviceDecision(
                "advice.v4", f"DEC-{actual_quantity}", "588080.SH", date(2026, 9, 2),
                date(2026, 9, 3), actual_quantity, 10_000, 10_000,
                10_000 - actual_quantity, "BUY" if order else "HOLD",
                STRATEGY, 1.5, 1.5,
                date(2026, 9, 2), order, () if order is None else (order,),
                available_cash, 0.0005, 0.0, available_cash,
            )

    store = PaperStore(tmp_path / "runtime.db")
    create_account(store, "a", "策略A")
    engine = VirtualAccountEngine(store, Advice())
    first = engine.refresh_account("a", date(2026, 9, 2), None)
    assert len(first["orders"]) == 1
    assert len(store.virtual_intents("a")) == 1
    second = engine.refresh_account(
        "a", date(2026, 9, 3),
        {"open": 1.49, "high": 1.52, "low": 1.48, "close": 1.51, "volume": 5_000_000},
    )
    assert second["quantity"] == 10_000
    assert Decimal(second["cash"]) == Decimal("985092.5500")
    assert len(second["fills"]) == 1
    assert second["metrics"]["observation_start"] == "2026-09-03"
    assert second["snapshots"][0]["total_assets"] == "1000192.5500"
    assert second["last_decision"]["contract_version"] == "advice.v4"
    assert second["observation_start"] == "2026-09-03"
    assert second["last_settlement_session"] == "2026-09-03"
    assert second["health"] == "OK"
    assert second["orders"][0]["payload"].find("diagnostic_participation") >= 0
    engine.refresh_account("a", date(2026, 9, 3), second["bar"])
    assert len(store.virtual_fills("a")) == 1


def test_bad_strategy_identity_blocks_only_that_virtual_account(tmp_path):
    from paper_trading_engine.contracts import AdviceDecision
    from paper_trading_engine.store import PaperStore
    from paper_trading_engine.virtual_engine import VirtualAccountEngine

    class Advice:
        def get_decision(
            self, actual_quantity, available_cash, cycle_target_quantity=None,
            strategy_id=None, strategy_version=None,
        ):
            sha = "b" * 64 if strategy_version == "v2" else "a" * 64
            return AdviceDecision(
                "advice.v4", f"DEC-{strategy_version}", "588080.SH", date(2026, 9, 2), date(2026, 9, 3),
                actual_quantity, 0, 0, -actual_quantity, "WAIT",
                {**STRATEGY, "version": strategy_version, "release_id": f"S001-{strategy_version}",
                 "release_hash": sha},
                1.5, 1.5, date(2026, 9, 2), None, (), available_cash, 0.0005, 0.0, available_cash,
            )

    store = PaperStore(tmp_path / "runtime.db")
    create_account(store, "broken", "错误身份", release_hash="c" * 64)
    create_account(store, "healthy", "正常身份")
    with store._connection:
        store._connection.execute(
            "UPDATE virtual_accounts SET strategy_version='v2' WHERE account_id='broken'"
        )
    engine = VirtualAccountEngine(store, Advice())

    results = engine.refresh_all(date(2026, 9, 2), None)

    assert [item["account_id"] for item in results] == ["healthy"]
    assert engine.status("broken")["health"] == "BLOCKED"
    assert engine.status("healthy")["health"] == "OK"


def test_failed_virtual_settlement_rolls_back_order_and_balances(tmp_path):
    from paper_trading_engine.store import PaperStore

    store = PaperStore(tmp_path / "runtime.db")
    store.create_virtual_account("a", "策略A", "baseline_a", "a" * 64, 1_000)
    store.save_virtual_order("a", "DEC", "2026-09-03", "ORDER", {
        "side": "BUY", "quantity": 1_000, "limit_price": 2.0,
    })

    with pytest.raises(ValueError, match="insufficient cash"):
        store.settle_virtual_order("a", "ORDER", "2026-09-03", Decimal("2.0"), Decimal("0.0005"))

    assert store.virtual_account("a")["cash"] == "1000.0000"
    assert store.virtual_orders("a")[0]["status"] == "PENDING"
    assert store.virtual_fills("a") == []


def test_pristine_virtual_account_capital_migration_rejects_trading_history(tmp_path) -> None:
    from paper_trading_engine.store import PaperStore

    store = PaperStore(tmp_path / "state.db")
    store.create_virtual_account("clean", "空白", "baseline_a", "a" * 64, 1_000_000)
    migrated = store.migrate_pristine_virtual_account_capital(
        "clean", expected_initial_cash=1_000_000, new_initial_cash=100_000,
    )
    assert migrated["initial_cash"] == "100000.0000"
    assert migrated["cash"] == "100000.0000"

    store.create_virtual_account("used", "已使用", "baseline_b", "b" * 64, 1_000_000)
    store.save_virtual_order(
        "used", "decision-1", "2026-09-03", "order-1",
        {"side": "BUY", "quantity": 100, "limit_price": 1.5},
    )
    with pytest.raises(ValueError, match="trading history"):
        store.migrate_pristine_virtual_account_capital(
            "used", expected_initial_cash=1_000_000, new_initial_cash=100_000,
        )
    assert store.virtual_account("used")["initial_cash"] == "1000000.0000"
    store.close()
