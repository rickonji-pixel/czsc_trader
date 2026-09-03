from __future__ import annotations

import json
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class FakeEngine:
    def __init__(self):
        self.paused = False
        self.cancelled = []

    def status(self):
        return {
            "environment": "SIMULATE",
            "market": "CN",
            "symbol": "588080.SH",
            "quote_health": "DEGRADED_QUOTE",
            "paused": self.paused,
            "orders": [{"channel_order_id": "1001", "status": "SUBMITTED"}],
        }

    def pause(self):
        self.paused = True
        return self.status()

    def resume(self):
        self.paused = False
        return self.status()

    def issue_cancel_token(self, order_id):
        return f"token-{order_id}"

    def confirm_cancel(self, order_id, token):
        if token != f"token-{order_id}":
            raise RuntimeError("invalid token")
        self.cancelled.append(order_id)
        return self.status()

    def pause_virtual(self, account_id):
        return {"account_id": account_id, "paused": True}

    def resume_virtual(self, account_id):
        return {"account_id": account_id, "paused": False}

    def system_status(self):
        return {"scope": {"system": "pte"}, "runtime": "RUNNING"}

    def virtual_accounts(self):
        return {"default_account_id": "alpha", "accounts": [{"account_id": "alpha"}]}

    def virtual_account_snapshot(self, account_id):
        if account_id != "alpha":
            from paper_trading_engine.web_api import ResourceNotFound
            raise ResourceNotFound(account_id)
        return {"scope": {"account_id": account_id}, "account": {"account_id": account_id}}

    def channel_snapshot(self, channel):
        return {"scope": {"channel": channel}, "orders": []}

    def comparison(self, account_ids):
        return {"scope": {"resource": "comparison"}, "accounts": [
            {"account_id": value} for value in account_ids
        ]}


def request_json(url: str, method: str = "GET", payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=3) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_http_status_pause_resume_and_two_step_cancel() -> None:
    from paper_trading_engine.web import create_server

    engine = FakeEngine()
    server = create_server(engine, host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert request_json(base + "/api/status")[1]["symbol"] == "588080.SH"
        assert request_json(base + "/api/pause", "POST", {})[1]["paused"] is True
        assert request_json(base + "/api/resume", "POST", {})[1]["paused"] is False
        token = request_json(
            base + "/api/cancel-token", "POST", {"channel_order_id": "1001"}
        )[1]["token"]
        request_json(
            base + "/api/cancel",
            "POST",
            {"channel_order_id": "1001", "token": token},
        )
        assert engine.cancelled == ["1001"]
        try:
            request_json(
                base + "/api/cancel",
                "POST",
                {"channel_order_id": "1001", "token": "wrong"},
            )
        except HTTPError as exc:
            assert exc.code == 409
        else:
            raise AssertionError("invalid cancellation must return conflict")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_dashboard_is_served_with_operations_controls() -> None:
    from paper_trading_engine.web import create_server

    server = create_server(FakeEngine(), host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=3) as response:
            html = response.read().decode("utf-8")
        assert "模拟交易控制台" in html
        assert 'href="/accounts/"' in html
        assert 'href="/channels/futu"' in html
        assert 'href="/comparison"' in html
        assert 'src="/static/app.js"' in html
        assert 'href="/static/styles.css"' in html
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_resume_failure_returns_visible_error_payload() -> None:
    from paper_trading_engine.web import create_server

    engine = FakeEngine()
    engine.resume = lambda: (_ for _ in ()).throw(RuntimeError("对账尚未成功"))
    server = create_server(engine, host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            request_json(
                f"http://127.0.0.1:{server.server_port}/api/resume", "POST", {}
            )
        except HTTPError as exc:
            assert exc.code == 409
            body = json.loads(exc.read().decode("utf-8"))
            assert body["error"] == "对账尚未成功"
        else:
            raise AssertionError("failed resume must return conflict")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_interventions_require_json_content_type() -> None:
    from paper_trading_engine.web import create_server

    engine = FakeEngine()
    server = create_server(engine, host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/pause",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "text/plain"},
        )
        try:
            urlopen(request, timeout=3)
        except HTTPError as exc:
            assert exc.code == 415
        else:
            raise AssertionError("non-JSON intervention must be rejected")
        assert engine.paused is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_virtual_account_pause_and_resume_routes() -> None:
    from paper_trading_engine.web import create_server

    server = create_server(FakeEngine(), host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        paused = request_json(base + "/api/virtual-accounts/alpha/pause", "POST", {})[1]
        resumed = request_json(base + "/api/virtual-accounts/alpha/resume", "POST", {})[1]
        assert paused["scope"]["account_id"] == "alpha"
        assert resumed["scope"]["account_id"] == "alpha"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_resource_routes_and_deep_links() -> None:
    from paper_trading_engine.web import create_server

    server = create_server(FakeEngine(), host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert request_json(base + "/api/system/status")[1]["scope"]["system"] == "pte"
        assert request_json(base + "/api/virtual-accounts")[1]["default_account_id"] == "alpha"
        assert request_json(base + "/api/virtual-accounts/alpha/snapshot")[1]["scope"]["account_id"] == "alpha"
        assert request_json(base + "/api/channels/futu/snapshot")[1]["scope"]["channel"] == "futu"
        assert request_json(base + "/api/comparison?account_id=alpha")[1]["accounts"][0]["account_id"] == "alpha"
        account_action = request_json(base + "/api/virtual-accounts/alpha/pause", "POST", {})[1]
        channel_action = request_json(base + "/api/channels/futu/pause", "POST", {})[1]
        assert account_action["scope"]["account_id"] == "alpha"
        assert channel_action["scope"]["channel"] == "futu"
        for path in ("/accounts/alpha", "/channels/futu", "/comparison"):
            with urlopen(base + path, timeout=3) as response:
                assert response.status == 200
        try:
            request_json(base + "/api/virtual-accounts/missing/snapshot")
        except HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("unknown account must return 404")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
