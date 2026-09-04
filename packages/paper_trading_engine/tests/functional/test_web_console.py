import json
from threading import Event, Thread
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from paper_trading_engine.web import create_server
from paper_trading_engine.audit import AuditRecorder
from paper_trading_engine.store import PaperStore
from paper_trading_engine.web_api import PteWebApi


class FakeEngine:
    def __init__(self): self.paused, self.cancelled, self.audit_filters = False, [], []
    def status(self):
        return {"environment": "SIMULATE", "market": "CN", "symbol": "588080.SH",
                "paused": self.paused, "orders": []}
    def pause(self):
        self.paused = True
        return self.status()
    def resume(self):
        self.paused = False
        return self.status()
    def issue_cancel_token(self, account_id, order_id): return f"token-{account_id}-{order_id}"
    def confirm_cancel(self, account_id, order_id, token):
        if token != f"token-{account_id}-{order_id}":
            raise RuntimeError("invalid token")
        self.cancelled.append(order_id)
        return self.status()
    def pause_virtual(self, account_id): return {"account_id": account_id, "paused": True}
    def resume_virtual(self, account_id): return {"account_id": account_id, "paused": False}
    def system_status(self): return {"scope": {"system": "pte"}, "runtime": "RUNNING"}
    def virtual_accounts(self): return {"default_account_id": "alpha", "accounts": [{"account_id": "alpha"}]}
    def virtual_account_snapshot(self, account_id):
        if account_id != "alpha":
            from paper_trading_engine.web_api import ResourceNotFound
            raise ResourceNotFound(account_id)
        return {"scope": {"account_id": account_id}}
    def channel_snapshot(self, channel): return {"scope": {"channel": channel}, "orders": []}
    def comparison(self, account_ids): return {"accounts": [{"account_id": x} for x in account_ids]}
    def audit_events(self, filters):
        self.audit_filters.append(filters)
        category = filters.get("category")
        if category and category not in {"STRATEGY", "TRADING", "SYSTEM", "OTHER"}:
            raise ValueError(f"invalid audit category: {category}")
        limit = int(filters.get("limit", "50"))
        if not 1 <= limit <= 200:
            raise ValueError("audit event limit must be between 1 and 200")
        return {
            "scope": {"resource": "audit_events"},
            "events": [{"event_id": "EV-1", "category": category or "SYSTEM"}],
            "next_before_id": "EV-1",
        }


def request_json(url, method="GET", payload=None, token=None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-PTE-Control-Token"] = token
    with urlopen(Request(url, data=data, method=method, headers=headers), timeout=3) as response:
        return response.status, json.loads(response.read())


def test_ft_pte05_console_resources_interventions_events_and_restart(tmp_path):
    store = PaperStore(tmp_path / "audit.db")
    recorder = AuditRecorder(store)
    recorder.record(
        "DECISION_GENERATED", source="test", account_id="alpha",
        strategy_id="S001", correlation_id="DEC-1",
    )
    audit_api = PteWebApi(SimpleNamespace(store=store, virtual=None, channel=None))
    count = len(store.recent_events())
    result = audit_api.audit_events({
        "category": "STRATEGY", "account_id": "alpha",
        "correlation_id": "DEC-1", "limit": "20",
    })
    assert result["events"][0]["event_type"] == "DECISION_GENERATED"
    assert len(store.recent_events()) == count
    with pytest.raises(ValueError, match="invalid audit category"):
        audit_api.audit_events({"category": "INVALID"})
    with pytest.raises(ValueError, match="invalid audit event_type"):
        audit_api.audit_events({"event_type": "UNKNOWN_EVENT"})
    with pytest.raises(ValueError, match="between 1 and 200"):
        audit_api.audit_events({"limit": "0"})
    store.close()

    requested, engine = Event(), FakeEngine()
    server = create_server(engine, host="127.0.0.1", port=0, control_token="secret",
                           restart_callback=requested.set, instance_id="old")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/", timeout=3) as response:
            html = response.read().decode()
        assert "模拟交易控制台" in html and "审计事件" in html
        assert html.index("Futu渠道") < html.index("审计事件") < html.index("账户比较")
        with urlopen(base + "/audit-events", timeout=3) as response:
            assert response.status == 200
        assert request_json(base + "/api/system/status")[1]["instance_id"] == "old"
        assert request_json(base + "/api/virtual-accounts")[1]["default_account_id"] == "alpha"
        assert request_json(base + "/api/virtual-accounts/alpha/snapshot")[1]["scope"]["account_id"] == "alpha"
        assert request_json(base + "/api/channels/futu/snapshot")[1]["scope"]["channel"] == "futu"
        audit = request_json(
            base + "/api/audit-events?category=STRATEGY&account_id=alpha&correlation_id=DEC-1&limit=20"
        )[1]
        assert audit["events"][0]["category"] == "STRATEGY"
        assert audit["next_before_id"] == "EV-1"
        assert engine.audit_filters[-1] == {
            "category": "STRATEGY", "account_id": "alpha",
            "correlation_id": "DEC-1", "limit": "20",
        }
        for query in ("category=INVALID", "limit=0"):
            with pytest.raises(HTTPError) as invalid:
                request_json(base + "/api/audit-events?" + query)
            assert invalid.value.code == 400
        assert request_json(base + "/api/pause", "POST", {})[1]["paused"] is True
        assert request_json(base + "/api/resume", "POST", {})[1]["paused"] is False
        token = request_json(base + "/api/cancel-token", "POST", {"account_id": "alpha", "channel_order_id": "1"})[1]["token"]
        request_json(base + "/api/cancel", "POST", {"account_id": "alpha", "channel_order_id": "1", "token": token})
        assert engine.cancelled == ["1"]
        with pytest.raises(HTTPError) as denied:
            request_json(base + "/api/system/restart", "POST", {}, "wrong")
        assert denied.value.code == 403
        assert request_json(base + "/api/system/restart", "POST", {}, "secret")[0] == 202
        assert requested.wait(1)
        with pytest.raises(HTTPError) as missing:
            request_json(base + "/api/virtual-accounts/missing/snapshot")
        assert missing.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
