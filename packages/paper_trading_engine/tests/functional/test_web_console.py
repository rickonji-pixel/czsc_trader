import json
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from paper_trading_engine.web import create_server


class FakeEngine:
    def __init__(self): self.paused, self.cancelled = False, []
    def status(self):
        return {"environment": "SIMULATE", "market": "CN", "symbol": "588080.SH",
                "paused": self.paused, "orders": []}
    def pause(self):
        self.paused = True
        return self.status()
    def resume(self):
        self.paused = False
        return self.status()
    def issue_cancel_token(self, order_id): return f"token-{order_id}"
    def confirm_cancel(self, order_id, token):
        if token != f"token-{order_id}":
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


def request_json(url, method="GET", payload=None, token=None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-PTE-Control-Token"] = token
    with urlopen(Request(url, data=data, method=method, headers=headers), timeout=3) as response:
        return response.status, json.loads(response.read())


def test_ft_pte05_console_resources_interventions_events_and_restart():
    requested, engine = Event(), FakeEngine()
    server = create_server(engine, host="127.0.0.1", port=0, control_token="secret",
                           restart_callback=requested.set, instance_id="old")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/", timeout=3) as response:
            html = response.read().decode()
        assert "模拟交易控制台" in html and "系统事件" in html
        assert request_json(base + "/api/system/status")[1]["instance_id"] == "old"
        assert request_json(base + "/api/virtual-accounts")[1]["default_account_id"] == "alpha"
        assert request_json(base + "/api/virtual-accounts/alpha/snapshot")[1]["scope"]["account_id"] == "alpha"
        assert request_json(base + "/api/channels/futu/snapshot")[1]["scope"]["channel"] == "futu"
        assert request_json(base + "/api/pause", "POST", {})[1]["paused"] is True
        assert request_json(base + "/api/resume", "POST", {})[1]["paused"] is False
        token = request_json(base + "/api/cancel-token", "POST", {"channel_order_id": "1"})[1]["token"]
        request_json(base + "/api/cancel", "POST", {"channel_order_id": "1", "token": token})
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
