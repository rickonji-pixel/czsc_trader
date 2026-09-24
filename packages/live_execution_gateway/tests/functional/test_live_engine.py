from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from paper_trading_engine.contracts import AdviceDecision
from strategy_runtime import TradableWindow, unavailable_observation

from live_execution_gateway.live_engine import LongbridgeLiveEngine
from live_execution_gateway.live_store import LiveTradeStore
from live_execution_gateway.longbridge_trading import LongbridgeSubmissionUncertain


NY = ZoneInfo("America/New_York")
RELEASE_HASH = "a" * 64


def _repo(path: Path, qualification: str) -> None:
    root = path / "strategies" / "S103"
    (root / "versions").mkdir(parents=True)
    (root / "versions" / "v1.json").write_text(json.dumps({
        "strategy_id": "S103", "version": "v1", "release_id": "S103-v1",
        "release_hash": RELEASE_HASH, "selection_data_cutoff": "2026-09-01",
    }), encoding="utf-8")
    (root / "lifecycle.jsonl").write_text(json.dumps({
        "strategy_id": "S103", "version": "v1", "release_hash": RELEASE_HASH,
        "to_state": qualification,
    }) + "\n", encoding="utf-8")


def _register(database: Path) -> None:
    store = LiveTradeStore(database)
    try:
        store.register_deployment(
            account_id="live-1", strategy_id="S103", strategy_version="v1",
            release_hash=RELEASE_HASH, symbol="MU.US",
            max_order_notional=Decimal("10000"),
            max_gross_notional=Decimal("20000"), cash_reserve=Decimal("100"),
        )
    finally:
        store.close()


def _snapshot(*, orders=None) -> dict:
    return {
        "captured_at": "2026-09-23T14:00:00+00:00", "symbol": "MU.US",
        "order_write_capability": False,
        "balances": [{"net_assets": "50000", "cash_infos": [{"currency": "USD", "available_cash": "50000"}]}],
        "positions": [], "today_orders": list(orders or []), "today_executions": [],
    }


class FakeReadGateway:
    def __init__(self, snapshot=None, history=None):
        self.value = snapshot or _snapshot()
        self.history_value = history or {
            "captured_at": "2026-09-23T14:00:00+00:00", "symbol": "MU.US",
            "order_write_capability": False, "orders": [], "executions": [],
        }

    def snapshot(self):
        return self.value

    def history(self, days=30):
        return self.history_value


class FakeTradingGateway:
    def __init__(self, error=None, cancel_error=None):
        self.error = error
        self.cancel_error = cancel_error
        self.requests = []
        self.cancellations = []

    def submit(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return "LB-ORDER-1"

    def cancel(self, order_id):
        self.cancellations.append(order_id)
        if self.cancel_error:
            raise self.cancel_error


class FakeAdvice:
    def __init__(self, qualification: str, source="SRT-1"):
        self.qualification = qualification
        self.source = source

    def latest_completed_signal_date(self, *, at):
        return date(2026, 9, 22)

    def prepare_account_data(self, **kwargs):
        return SimpleNamespace(tradable_window=TradableWindow(date(2026, 9, 23), date(2026, 9, 23)))

    def get_decision(self, actual_quantity, available_cash, total_assets=50000, **kwargs):
        payload = {
            "status": "PASS",
            "result": {
                "contract_version": "advice.v4", "decision_id": self.source,
                "source_decision_id": self.source, "symbol": "MU.US",
                "signal_date": "2026-09-22", "valid_session": "2026-09-23",
                "actual_quantity": actual_quantity, "target_quantity": 2,
                "cycle_target_quantity": 2, "delta_quantity": 2 - actual_quantity,
                "action": "BUY", "strategy": {
                    "strategy_id": "S103", "version": "v1", "release_id": "S103-v1",
                    "release_hash": RELEASE_HASH, "qualification": self.qualification,
                    "name": "test",
                },
                "signal_reference_price": 100.0, "execution_reference_price": 100.0,
                "data_cutoff": "2026-09-22",
                "order": {
                    "side": "BUY", "quantity": 2, "order_type": "MARKET",
                    "limit_price": 100.0, "time_in_force": "DAY",
                },
                "orders": [{
                    "side": "BUY", "quantity": 2, "order_type": "MARKET",
                    "limit_price": 100.0, "time_in_force": "DAY",
                }],
                "available_cash": available_cash, "fee_rate": 0.001,
                "estimated_order_cost": 200.2, "unallocated_cash": available_cash - 200.2,
                "capital_mode": "available_cash_fraction", "allocation_fraction": 0.98,
                "plan_mode": "NONE", "plan_legs": [], "runtime_sha256": "b" * 64,
                "input_identity_hashes": {"mu_daily": "c" * 64},
                "signal_identity": "d" * 64, "plan_identity": "e" * 64,
                "portfolio_revision": 0, "state_revision": 0,
                "strategy_output": {"signal_date": "2026-09-22"},
                "observation": unavailable_observation("test"),
            },
        }
        return AdviceDecision.from_cli_payload(payload)


def _engine(tmp_path: Path, qualification: str, trading=None) -> LongbridgeLiveEngine:
    _repo(tmp_path, qualification)
    database = tmp_path / "live.db"
    _register(database)
    engine = LongbridgeLiveEngine(
        repo_root=tmp_path, data_dir=tmp_path / "data", database=database,
        env_file=tmp_path / ".env", account_id="live-1",
        read_gateway=FakeReadGateway(),
        trading_gateway=trading or FakeTradingGateway(),
        now=lambda: datetime(2026, 9, 23, 10, 0, tzinfo=NY),
    )
    engine.advice = FakeAdvice(qualification)
    return engine


def _arm(engine: LongbridgeLiveEngine) -> None:
    challenge = engine.trade_store.issue_arm_challenge("live-1")
    engine.trade_store.arm(
        "live-1", challenge_id=challenge["challenge_id"],
        confirmation_phrase=challenge["confirmation_phrase"], hours=1,
        actor="owner", reason="test",
    )


def test_paper_ready_can_only_create_shadow_plan(tmp_path: Path) -> None:
    trading = FakeTradingGateway()
    engine = _engine(tmp_path, "PAPER_READY", trading)
    try:
        result = engine.cycle()
    finally:
        engine.close()

    assert result["planning"]["disposition"] == "SHADOW_ONLY"
    assert result["submission"] is None
    assert trading.requests == []


def test_live_ready_still_requires_arming_then_submits_once(tmp_path: Path) -> None:
    trading = FakeTradingGateway()
    engine = _engine(tmp_path, "LIVE_READY", trading)
    try:
        disarmed = engine.cycle()
        assert disarmed["planning"]["disposition"] == "SHADOW_ONLY"
        engine.advice = FakeAdvice("LIVE_READY", source="SRT-2")
        _arm(engine)
        armed = engine.cycle()
        repeated = engine.cycle()
    finally:
        engine.close()

    assert armed["planning"]["disposition"] == "READY"
    assert armed["submission"]["order_id"] == "LB-ORDER-1"
    assert repeated["submission"] is None
    assert len(trading.requests) == 1
    assert trading.requests[0].client_request_id.startswith("CZSC-")


def test_uncertain_submission_blocks_and_disarms(tmp_path: Path) -> None:
    trading = FakeTradingGateway(LongbridgeSubmissionUncertain("timeout"))
    engine = _engine(tmp_path, "LIVE_READY", trading)
    try:
        _arm(engine)
        with pytest.raises(LongbridgeSubmissionUncertain):
            engine.cycle()
        status = engine.trade_store.status("live-1")
        intents = engine.trade_store.intents("live-1")
    finally:
        engine.close()

    assert status["status"] == "BLOCKED"
    assert intents[0]["status"] == "SUBMISSION_UNCERTAIN"


def test_existing_rest_status_order_blocks_new_submission(tmp_path: Path) -> None:
    trading = FakeTradingGateway()
    engine = _engine(tmp_path, "LIVE_READY", trading)
    try:
        _arm(engine)
        engine.read_gateway = FakeReadGateway(snapshot=_snapshot(orders=[{
            "order_id": "MANUAL-1", "symbol": "MU.US", "status": "NewStatus",
        }]))
        with pytest.raises(Exception, match="active order"):
            engine.cycle()
    finally:
        engine.close()

    assert trading.requests == []


def test_prior_day_active_broker_order_blocks_new_submission(tmp_path: Path) -> None:
    trading = FakeTradingGateway()
    engine = _engine(tmp_path, "LIVE_READY", trading)
    try:
        _arm(engine)
        engine.read_gateway = FakeReadGateway(history={
            "captured_at": "2026-09-23T14:00:00+00:00", "symbol": "MU.US",
            "order_write_capability": False, "orders": [{
                "order_id": "OLD-GTC", "symbol": "MU.US", "status": "NewStatus",
            }], "executions": [],
        })
        with pytest.raises(Exception, match="active order"):
            engine.cycle()
    finally:
        engine.close()

    assert trading.requests == []


def test_restart_reconciles_submitting_intent_by_durable_remark(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "LIVE_READY")
    try:
        _arm(engine)
        engine.plan(_snapshot())
        intent = engine.trade_store.ready_intents("live-1")[0]
        engine.trade_store.claim_intent(intent["intent_id"])
        order = {
            "order_id": "LB-RECOVERED", "remark": intent["intent_id"], "symbol": "MU.US",
            "side": "Buy", "quantity": "2", "status": "NewStatus",
        }
        engine.read_gateway = FakeReadGateway(
            snapshot=_snapshot(orders=[order]),
            history={
                "captured_at": "2026-09-23T14:00:00+00:00", "symbol": "MU.US",
                "order_write_capability": False, "orders": [order], "executions": [],
            },
        )
        result = engine.startup_reconcile()
        recovered = engine.trade_store.intent(intent["intent_id"])
    finally:
        engine.close()

    assert result["intents_reconciled"] == 1
    assert recovered["broker_order_id"] == "LB-RECOVERED"
    assert recovered["status"] == "SUBMITTED"


def test_cycle_uses_history_for_cross_day_unresolved_order(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "LIVE_READY")
    try:
        _arm(engine)
        engine.plan(_snapshot())
        intent = engine.trade_store.ready_intents("live-1")[0]
        engine.trade_store.claim_intent(intent["intent_id"])
        order = {
            "order_id": "LB-YESTERDAY", "remark": intent["intent_id"],
            "symbol": "MU.US", "side": "Buy", "quantity": "2", "status": "FilledStatus",
        }
        engine.read_gateway = FakeReadGateway(
            snapshot=_snapshot(),
            history={
                "captured_at": "2026-09-23T14:00:00+00:00", "symbol": "MU.US",
                "order_write_capability": False, "orders": [order], "executions": [],
            },
        )

        result = engine.reconcile(_snapshot())
        recovered = engine.trade_store.intent(intent["intent_id"])
    finally:
        engine.close()

    assert result["intents_reconciled"] == 1
    assert recovered["broker_order_id"] == "LB-YESTERDAY"
    assert recovered["status"] == "FILLED"


def test_live_session_windows_cover_pre_regular_post_and_next_day_overnight(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, "PAPER_READY")
    try:
        cases = [
            (datetime(2026, 9, 23, 4, 30, tzinfo=NY), "2026-09-23", "ANY_TIME", True),
            (datetime(2026, 9, 23, 10, 0, tzinfo=NY), "2026-09-23", "RTH_ONLY", True),
            (datetime(2026, 9, 23, 19, 0, tzinfo=NY), "2026-09-23", "ANY_TIME", True),
            (datetime(2026, 9, 23, 20, 30, tzinfo=NY), "2026-09-24", "OVERNIGHT", True),
            (datetime(2026, 9, 24, 3, 30, tzinfo=NY), "2026-09-24", "OVERNIGHT", True),
            (datetime(2026, 9, 24, 3, 55, tzinfo=NY), "2026-09-24", "OVERNIGHT", False),
        ]
        for moment, valid_session, outside_rth, expected in cases:
            engine.now = lambda moment=moment: moment
            assert engine._submission_window({
                "valid_session": valid_session,
                "outside_rth": outside_rth,
                "order_type": "LIMIT",
            }) is expected
    finally:
        engine.close()


@pytest.mark.parametrize("moment,valid_session,session", [
    (datetime(2026, 9, 23, 4, 30, tzinfo=NY), "2026-09-23", "ANY_TIME"),
    (datetime(2026, 9, 23, 20, 30, tzinfo=NY), "2026-09-24", "OVERNIGHT"),
])
def test_explicit_extended_session_intent_reaches_gateway(
    tmp_path: Path, moment: datetime, valid_session: str, session: str,
) -> None:
    trading = FakeTradingGateway()
    engine = _engine(tmp_path, "LIVE_READY", trading)
    try:
        _arm(engine)
        decision = asdict(FakeAdvice("LIVE_READY").get_decision(0, 50000))
        decision["valid_session"] = valid_session
        decision["order"]["order_type"] = "LIMIT"
        decision["order"]["outside_rth"] = session
        decision["orders"][0]["order_type"] = "LIMIT"
        decision["orders"][0]["outside_rth"] = session
        engine.trade_store.save_decision_and_intents(
            account_id="live-1", decision=decision,
            qualification="LIVE_READY", executable=True,
        )
        engine.now = lambda: moment
        result = engine.submit_ready(_snapshot())
    finally:
        engine.close()

    assert result["order_id"] == "LB-ORDER-1"
    assert trading.requests[0].outside_rth == session


def test_cancel_is_limited_to_owned_order_and_persisted(tmp_path: Path) -> None:
    trading = FakeTradingGateway()
    engine = _engine(tmp_path, "LIVE_READY", trading)
    try:
        _arm(engine)
        engine.cycle()
        with pytest.raises(PermissionError, match="owned"):
            engine.cancel("SOMEONE-ELSES-ORDER", confirmation="CANCEL_LONG_BRIDGE_REAL_ORDER")
        engine.cancel("LB-ORDER-1", confirmation="CANCEL_LONG_BRIDGE_REAL_ORDER")
        intent = engine.trade_store.intent_by_order("live-1", "LB-ORDER-1")
    finally:
        engine.close()

    assert trading.cancellations == ["LB-ORDER-1"]
    assert intent["status"] == "CANCEL_PENDING"


def test_uncertain_cancel_blocks_new_orders(tmp_path: Path) -> None:
    trading = FakeTradingGateway(
        cancel_error=LongbridgeSubmissionUncertain("cancel timeout"),
    )
    engine = _engine(tmp_path, "LIVE_READY", trading)
    try:
        _arm(engine)
        engine.cycle()
        with pytest.raises(LongbridgeSubmissionUncertain, match="cancel timeout"):
            engine.cancel("LB-ORDER-1", confirmation="CANCEL_LONG_BRIDGE_REAL_ORDER")
        status = engine.trade_store.status("live-1")
        intent = engine.trade_store.intent_by_order("live-1", "LB-ORDER-1")
    finally:
        engine.close()

    assert status["status"] == "BLOCKED"
    assert intent["status"] == "CANCEL_UNCERTAIN"
