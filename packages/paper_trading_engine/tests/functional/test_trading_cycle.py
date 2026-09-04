from dataclasses import replace
from datetime import date
from subprocess import CompletedProcess

import pytest

from paper_trading_engine.account_engine import AccountEngine
from paper_trading_engine.advice_client import AdviceClientError, CliAdviceClient
from paper_trading_engine.audit import AuditRecorder
from paper_trading_engine.contracts import OrderSpec
from paper_trading_engine.futu_execution import FutuExecution
from paper_trading_engine.store import PaperStore
from pte_support import FakeAdvice, FakeBroker, broker_snapshot, decision


def test_ft_pte02_account_decision_futu_order_fill_restart_and_idempotence(tmp_path):
    store = PaperStore(tmp_path / "runtime.db")
    store.create_virtual_account(
        "s001-v1", "S001-v1模拟账户", "legacy", "a" * 64, 100_000,
        strategy_id="S001", strategy_name_snapshot="综合基线策略",
        strategy_version="v1", release_hash="b" * 64,
        qualification_snapshot="PAPER_READY", selection_data_cutoff="2026-09-01",
    )
    advice = FakeAdvice(decision(OrderSpec("BUY", 1000, "LIMIT", 1.68, "DAY")))
    accounts = AccountEngine(store, advice)
    broker = FakeBroker()
    execution = FutuExecution(store, broker, symbol="588080.SH", today=lambda: date(2026, 9, 2))

    accounts.refresh_account("s001-v1", force=True)
    accounts.refresh_account("s001-v1", force=True)
    assert len(store.account_decisions("s001-v1")) == 1
    assert len(store.pending_account_intents()) == 1
    snapshot = store.account_snapshots("s001-v1")[0]
    assert snapshot["session"] == "2026-09-01"
    assert float(snapshot["total_assets"]) == 100_000
    decision_event = store.query_audit_events(
        event_type="DECISION_GENERATED", account_id="s001-v1"
    )[0]
    assert decision_event["channel"] is None

    execution.refresh_account()
    execution.submit_pending()
    execution.submit_pending()
    assert len(broker.placed) == 1
    submitted = broker.value.orders[0]
    broker.value = broker_snapshot(
        orders=(replace(
            submitted, status="FILLED_PART", cumulative_filled_quantity=400,
            average_fill_price=1.67,
        ),), quantity=400,
    )
    execution.refresh_orders()
    execution.refresh_orders()
    assert store.virtual_account("s001-v1")["quantity"] == 400
    assert len(store.account_fills("s001-v1")) == 1

    broker.value = broker_snapshot(
        orders=(replace(
            submitted, status="FILLED_ALL", cumulative_filled_quantity=1000,
            average_fill_price=1.68,
        ),), quantity=1000,
    )
    execution.refresh_orders()
    assert store.virtual_account("s001-v1")["quantity"] == 1000
    assert sum(row["quantity"] for row in store.account_fills("s001-v1")) == 1000
    assert len(store.query_audit_events(event_type="ORDER_FILLED", account_id="s001-v1")) == 1
    store.close()

    reopened = PaperStore(tmp_path / "runtime.db")
    assert reopened.virtual_account("s001-v1")["quantity"] == 1000
    assert len(reopened.account_orders("s001-v1")) == 1
    assert len(reopened.account_fills("s001-v1")) == 2
    reopened.close()

    audit_store = PaperStore(tmp_path / "advice-failure.db")
    client = CliAdviceClient(
        executable="czsc-trader", repo_root=tmp_path, data_dir=tmp_path,
        symbol="588080.SH", asset="etf", audit=AuditRecorder(audit_store),
        runner=lambda args, **kwargs: CompletedProcess(args, 5, "", "provider unavailable"),
    )
    with pytest.raises(AdviceClientError):
        client.get_decision(
            0, 100_000, strategy_id="S001", strategy_version="v1",
            account_id="s001-v1",
        )
    failed = audit_store.query_audit_events(event_type="DECISION_GENERATION_FAILED")[0]
    assert failed["account_id"] == "s001-v1"
    external = audit_store.query_audit_events(event_type="EXTERNAL_CALL_FAILED")[0]
    assert external["account_id"] == "s001-v1"
    audit_store.close()
