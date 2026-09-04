import json
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from paper_trading_engine.audit import AuditContractError, AuditEvent, AuditRecorder
from paper_trading_engine.performance_export import export_performance
from paper_trading_engine.store import PaperStore


def test_ft_pte07_performance_evidence_is_self_contained_and_release_bound(tmp_path):
    class CaptureStore:
        def __init__(self):
            self.events = []

        def append_audit_event(self, event):
            self.events.append(event)
            return event.to_dict()

    capture = CaptureStore()
    recorder = AuditRecorder(capture)
    audit = recorder.record(
        "DECISION_GENERATED",
        source="engine",
        decision_id="DEC-1",
        details={"token": "secret-token", "nested": {"password": "secret"}},
    )
    assert audit["category"] == "STRATEGY"
    assert audit["severity"] == "INFO"
    assert audit["details"] == {
        "token": "[REDACTED]",
        "nested": {"password": "[REDACTED]"},
    }
    assert audit["correlation_id"] == "DEC-1"
    with pytest.raises(AuditContractError, match="catalog"):
        recorder.record("UNKNOWN", source="engine")
    with pytest.raises(AuditContractError, match="classification_reason"):
        recorder.record("UNCLASSIFIED_EVENT", source="engine", details={})
    with pytest.raises(FrozenInstanceError):
        capture.events[0].event_type = "CHANGED"
    assert isinstance(capture.events[0], AuditEvent)

    store = PaperStore(tmp_path / "runtime.db")
    store.create_virtual_account(
        "s001-forward", "S001-v1模拟账户", "baseline", "a" * 64, 100_000,
        strategy_id="S001", strategy_name_snapshot="综合基线策略", strategy_version="v1",
        release_hash="b" * 64, qualification_snapshot="PAPER_READY",
    )
    for session, side, price in (("2026-09-03", "BUY", "1.0"), ("2026-09-04", "SELL", "1.1")):
        order_id = f"ORDER-{side}"
        store.save_virtual_order("s001-forward", f"DEC-{side}", session, order_id,
                                 {"side": side, "quantity": 1000, "limit_price": float(price), "fee_rate": 0.0005})
        store.settle_virtual_order("s001-forward", order_id, session, Decimal(price), Decimal("0.0005"))
        account = store.virtual_account("s001-forward")
        store.save_virtual_snapshot("s001-forward", session, {
            "close": price, "cash": account["cash"], "quantity": account["quantity"],
            "total_assets": str(Decimal(account["cash"]) + Decimal(price) * account["quantity"]),
        })
    output = tmp_path / "evidence.json"
    result = export_performance(store, "s001-forward", output, recorded_by="tester")
    assert json.loads(output.read_text(encoding="utf-8")) == result
    assert result["evidence"]["phase"] == "PAPER_FORWARD"
    assert result["evidence"]["strategy_id"] == "S001"
    assert result["evidence"]["closed_trades"] == 1
    assert result["evidence"]["win_loss_ratio_status"] == "NO_LOSSES"
    assert len(result["evidence"]["source_hash"]) == 64
    store.close()
