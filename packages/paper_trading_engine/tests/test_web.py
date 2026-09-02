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
        assert "Paper Trading Engine" in html
        assert "暂停新订单" in html
        assert "/api/status" in html
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
