from decimal import Decimal


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
