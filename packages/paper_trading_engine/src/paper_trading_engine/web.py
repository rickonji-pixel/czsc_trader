"""Local-only HTTP server for the resource-scoped PTE console."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import mimetypes
import secrets
from threading import Thread
from urllib.parse import parse_qs, unquote, urlparse
from typing import Protocol
from uuid import uuid4

from .web_api import PteWebApi, ResourceNotFound


class Operations(Protocol):
    def status(self) -> dict[str, object]: ...


def create_server(
    operations: Operations, *, host: str = "127.0.0.1", port: int = 8080,
    control_token: str | None = None, restart_callback=None, instance_id: str | None = None,
):
    api = operations if hasattr(operations, "system_status") else PteWebApi(operations)
    static_root = files("paper_trading_engine").joinpath("static")
    runtime_instance_id = instance_id or uuid4().hex

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: object) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")

        def _resource(self, name: str) -> None:
            resource = static_root.joinpath(name)
            if not resource.is_file():
                self._json(404, {"error": "not found"})
                return
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self._send(200, resource.read_bytes(), f"{content_type}; charset=utf-8")

        def _body(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1_000_000:
                raise ValueError("request body too large")
            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value, dict):
                raise ValueError("request body must be an object")
            return value

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                if path == "/api/status":
                    self._json(200, operations.status())
                elif path == "/api/system/status":
                    self._json(200, {**api.system_status(), "instance_id": runtime_instance_id})
                elif path == "/api/virtual-accounts":
                    self._json(200, api.virtual_accounts())
                elif path.startswith("/api/virtual-accounts/") and path.endswith("/snapshot"):
                    account_id = unquote(path[len("/api/virtual-accounts/"):-len("/snapshot")].strip("/"))
                    self._json(200, api.virtual_account_snapshot(account_id))
                elif path == "/api/channels/futu/snapshot":
                    self._json(200, api.channel_snapshot("futu"))
                elif path == "/api/comparison":
                    self._json(200, api.comparison(parse_qs(parsed.query).get("account_id", [])))
                elif path.startswith("/static/"):
                    self._resource(path.removeprefix("/static/"))
                elif path == "/" or path == "/comparison" or path.startswith("/accounts/") or path == "/channels/futu":
                    self._resource("index.html")
                else:
                    self._json(404, {"error": "not found"})
            except ResourceNotFound as exc:
                self._json(404, {"error": f"unknown resource: {exc.args[0]}"})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})

        def do_POST(self) -> None:
            if self.headers.get_content_type() != "application/json":
                self._json(415, {"error": "application/json is required"})
                return
            try:
                body = self._body()
                path = urlparse(self.path).path
                parts = path.strip("/").split("/")
                if path == "/api/system/restart":
                    supplied = self.headers.get("X-PTE-Control-Token", "")
                    if control_token is None or not secrets.compare_digest(supplied, control_token):
                        self._json(403, {"error": "invalid PTE control token"})
                        return
                    if restart_callback is None:
                        self._json(409, {"error": "restart control is unavailable"})
                        return
                    self._json(202, {"status": "RESTART_ACCEPTED", "instance_id": runtime_instance_id})
                    Thread(target=restart_callback, name="pte-graceful-restart", daemon=True).start()
                    return
                if path in {"/api/pause", "/api/channels/futu/pause"}:
                    result = operations.pause()
                    if path.startswith("/api/channels/"):
                        result = api.channel_snapshot("futu")
                elif path in {"/api/resume", "/api/channels/futu/resume"}:
                    result = operations.resume()
                    if path.startswith("/api/channels/"):
                        result = api.channel_snapshot("futu")
                elif path in {"/api/cancel-token", "/api/channels/futu/cancel-token"}:
                    result = {"token": operations.issue_cancel_token(str(body["channel_order_id"]))}
                elif path in {"/api/cancel", "/api/channels/futu/cancel"}:
                    result = operations.confirm_cancel(str(body["channel_order_id"]), str(body["token"]))
                elif parts[:2] == ["api", "virtual-accounts"] and len(parts) == 4:
                    account_id = unquote(parts[2])
                    if parts[3] == "pause":
                        result = operations.pause_virtual(account_id)
                    elif parts[3] == "resume":
                        result = operations.resume_virtual(account_id)
                    else:
                        self._json(404, {"error": "not found"})
                        return
                    result = api.virtual_account_snapshot(account_id)
                else:
                    self._json(404, {"error": "not found"})
                    return
                self._json(200, result)
            except KeyError as exc:
                self._json(404, {"error": f"unknown resource: {exc.args[0]}"})
            except Exception as exc:
                self._json(409, {"error": str(exc)})

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, int(port)), Handler)
