from decimal import Decimal
from datetime import date
import pytest


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
        def get_decision(self, actual_quantity, available_cash, cycle_target_quantity=None, baseline=None):
            order = OrderSpec("BUY", 10_000, "LIMIT", 1.5, "DAY") if actual_quantity == 0 else None
            return AdviceDecision(
                "advice.v3", f"DEC-{actual_quantity}", "588080.SH", date(2026, 9, 2),
                date(2026, 9, 3), actual_quantity, 10_000, 10_000,
                10_000 - actual_quantity, "BUY" if order else "HOLD",
                {"version": baseline, "sha256": "a" * 64}, 1.5, 1.5,
                date(2026, 9, 2), order, () if order is None else (order,),
                available_cash, 0.0005, 0.0, available_cash,
            )

    store = PaperStore(tmp_path / "runtime.db")
    store.create_virtual_account("a", "策略A", "baseline_20260903", "a" * 64, Decimal("1000000"))
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
    assert second["last_decision"]["contract_version"] == "advice.v3"
    assert second["observation_start"] == "2026-09-03"
    assert second["last_settlement_session"] == "2026-09-03"
    assert second["health"] == "OK"
    assert second["orders"][0]["payload"].find("diagnostic_participation") >= 0
    engine.refresh_account("a", date(2026, 9, 3), second["bar"])
    assert len(store.virtual_fills("a")) == 1


def test_bad_baseline_identity_blocks_only_that_virtual_account(tmp_path):
    from paper_trading_engine.contracts import AdviceDecision
    from paper_trading_engine.store import PaperStore
    from paper_trading_engine.virtual_engine import VirtualAccountEngine

    class Advice:
        def get_decision(self, actual_quantity, available_cash, cycle_target_quantity=None, baseline=None):
            sha = "b" * 64 if baseline == "bad" else "a" * 64
            return AdviceDecision(
                "advice.v3", f"DEC-{baseline}", "588080.SH", date(2026, 9, 2), date(2026, 9, 3),
                actual_quantity, 0, 0, -actual_quantity, "WAIT", {"version": baseline, "sha256": sha},
                1.5, 1.5, date(2026, 9, 2), None, (), available_cash, 0.0005, 0.0, available_cash,
            )

    store = PaperStore(tmp_path / "runtime.db")
    store.create_virtual_account("broken", "错误身份", "bad", "a" * 64, 1_000_000)
    store.create_virtual_account("healthy", "正常身份", "good", "a" * 64, 1_000_000)
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
