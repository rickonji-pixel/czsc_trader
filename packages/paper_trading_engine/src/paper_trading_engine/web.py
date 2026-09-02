"""Local-only HTTP operations console."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from urllib.parse import unquote, urlparse
from typing import Protocol

from .dashboard import DASHBOARD


class Operations(Protocol):
    def status(self) -> dict[str, object]: ...
    def pause(self) -> dict[str, object]: ...
    def resume(self) -> dict[str, object]: ...
    def issue_cancel_token(self, channel_order_id: str) -> str: ...
    def confirm_cancel(self, channel_order_id: str, token: str) -> dict[str, object]: ...
    def pause_virtual(self, account_id: str) -> dict[str, object]: ...
    def resume_virtual(self, account_id: str) -> dict[str, object]: ...


def create_server(
    operations: Operations, *, host: str = "127.0.0.1", port: int = 8080
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1_000_000:
                raise ValueError("request body too large")
            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value, dict):
                raise ValueError("request body must be an object")
            return value

        def do_GET(self) -> None:
            if self.path == "/":
                body = DASHBOARD.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/status":
                self._json(200, operations.status())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.headers.get_content_type() != "application/json":
                self._json(415, {"error": "application/json is required"})
                return
            try:
                body = self._body()
                if self.path == "/api/pause":
                    result = operations.pause()
                elif self.path == "/api/resume":
                    result = operations.resume()
                elif self.path == "/api/cancel-token":
                    result = {"token": operations.issue_cancel_token(str(body["channel_order_id"]))}
                elif self.path == "/api/cancel":
                    result = operations.confirm_cancel(
                        str(body["channel_order_id"]), str(body["token"])
                    )
                elif (parts := urlparse(self.path).path.strip("/").split("/"))[:2] == ["api", "virtual-accounts"] and len(parts) == 4:
                    account_id = unquote(parts[2])
                    if parts[3] == "pause":
                        result = operations.pause_virtual(account_id)
                    elif parts[3] == "resume":
                        result = operations.resume_virtual(account_id)
                    else:
                        self._json(404, {"error": "not found"}); return
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
