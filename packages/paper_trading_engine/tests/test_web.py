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
        assert 'role="switch"' in html
        assert 'id="runSwitch"' in html
        assert 'id="pauseButton"' not in html
        assert 'id="resumeButton"' not in html
        assert "自动运行" in html
        assert "/api/status" in html
        assert "恢复成功" in html
        assert "DEGRADED_QUOTE:'行情降级'" in html
        assert "DATA_PUBLICATION_FAILED:'完整收盘数据发布失败'" in html
        assert "RESUMED:'已恢复自动运行'" in html
        assert "OUTSIDE_SUBMISSION_WINDOW:'当前不在自动发单时段'" in html
        assert 'id="account"' not in html
        assert 'id="decision"' not in html
        assert 'id="rawDetails"' in html
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
