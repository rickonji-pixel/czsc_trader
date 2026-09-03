import json
from decimal import Decimal


def _seed_account(store):
    store.create_virtual_account(
        "s001-forward",
        "S001前瞻账户",
        "baseline_20260903",
        "a7af8864e469b72a94c59eb2e012af5f9a634203cdf5a0214391dd2909e9e331",
        100_000,
        strategy_id="S001",
        strategy_name_snapshot="综合基线策略",
        strategy_version="v1",
        release_hash="ae422915ff736431d70e0381dd6514ee800d861060cc5568712b55c895ddfb62",
        qualification_snapshot="PAPER_READY",
    )
    store.save_virtual_order(
        "s001-forward",
        "DEC-BUY",
        "2026-09-03",
        "ORDER-BUY",
        {"side": "BUY", "quantity": 1_000, "limit_price": 1.0, "fee_rate": 0.0005},
    )
    store.settle_virtual_order(
        "s001-forward", "ORDER-BUY", "2026-09-03", Decimal("1.0"), Decimal("0.0005")
    )
    store.save_virtual_snapshot(
        "s001-forward",
        "2026-09-03",
        {"close": "1.0", "cash": "98999.5000", "quantity": 1_000, "total_assets": "99999.5000"},
    )
    store.save_virtual_order(
        "s001-forward",
        "DEC-SELL",
        "2026-09-04",
        "ORDER-SELL",
        {"side": "SELL", "quantity": 1_000, "limit_price": 1.1, "fee_rate": 0.0005},
    )
    store.settle_virtual_order(
        "s001-forward", "ORDER-SELL", "2026-09-04", Decimal("1.1"), Decimal("0.0005")
    )
    store.save_virtual_snapshot(
        "s001-forward",
        "2026-09-04",
        {"close": "1.1", "cash": "100098.9500", "quantity": 0, "total_assets": "100098.9500"},
    )


def test_performance_export_is_self_contained_and_bound_to_release(tmp_path):
    from paper_trading_engine.performance_export import export_performance
    from paper_trading_engine.store import PaperStore

    store = PaperStore(tmp_path / "runtime.db")
    _seed_account(store)
    output = tmp_path / "paper-forward.json"

    exported = export_performance(
        store,
        "s001-forward",
        output,
        recorded_by="tester",
        start="2026-09-03",
        end="2026-09-04",
    )
    persisted = json.loads(output.read_text(encoding="utf-8"))

    assert persisted == exported
    assert exported["evidence"]["phase"] == "PAPER_FORWARD"
    assert exported["evidence"]["strategy_id"] == "S001"
    assert exported["evidence"]["version"] == "v1"
    assert exported["evidence"]["initial_capital"] == 100_000.0
    assert exported["evidence"]["fee_rate"] == 0.0005
    assert exported["evidence"]["closed_trades"] == 1
    assert exported["evidence"]["win_loss_ratio_status"] == "NO_LOSSES"
    assert exported["evidence"]["maximum_drawdown"] <= 0
    assert "calmar_ratio" in exported["evidence"]
    assert "sharpe_ratio" in exported["evidence"]
    assert len(exported["evidence"]["source_hash"]) == 64
    assert len(exported["source"]["snapshots"]) == 2
    assert len(exported["source"]["closed_trade_pnl"]) == 1
    store.close()
