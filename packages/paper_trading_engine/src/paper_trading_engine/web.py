"""Local-only HTTP operations console."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from typing import Protocol


class Operations(Protocol):
    def status(self) -> dict[str, object]: ...

    def pause(self) -> dict[str, object]: ...

    def resume(self) -> dict[str, object]: ...

    def issue_cancel_token(self, channel_order_id: str) -> str: ...

    def confirm_cancel(self, channel_order_id: str, token: str) -> dict[str, object]: ...


DASHBOARD = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper Trading Engine</title><style>
:root{color-scheme:dark;--bg:#08111d;--card:#101d2c;--line:#26384d;--text:#e8f0f8;--muted:#8fa4b8;--ok:#53d39b;--warn:#f2bd5a;--danger:#ff6b72}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top right,#16314b 0,var(--bg) 42%);color:var(--text);font:14px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace}
main{max-width:1180px;margin:auto;padding:30px 20px}header{display:flex;align-items:end;justify-content:space-between;margin-bottom:20px}h1{font:600 28px/1.1 system-ui;margin:0}small,.muted{color:var(--muted)}.badge{padding:5px 9px;border:1px solid var(--line);border-radius:99px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.card{background:color-mix(in srgb,var(--card) 92%,transparent);border:1px solid var(--line);border-radius:12px;padding:16px}.wide{grid-column:span 2}.full{grid-column:1/-1}.label{color:var(--muted);font-size:12px;text-transform:uppercase}.value{font:600 20px system-ui;margin-top:5px}button{background:#19334a;color:var(--text);border:1px solid #36526c;border-radius:8px;padding:9px 12px;cursor:pointer}button.danger{background:#4a2028;border-color:#7d3540}.actions{display:flex;gap:8px;margin:14px 0 20px}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--line)}pre{white-space:pre-wrap;max-height:260px;overflow:auto;color:#bdd0df}@media(max-width:760px){.grid{grid-template-columns:1fr 1fr}.wide{grid-column:span 2}}
</style></head><body><main><header><div><h1>Paper Trading Engine</h1><small>本机模拟交易观测与干预</small></div><span class="badge" id="health">连接中</span></header>
<div class="actions"><button onclick="post('/api/pause',{})">暂停新订单</button><button onclick="post('/api/resume',{})">恢复运行</button></div>
<section class="grid"><div class="card"><div class="label">环境</div><div class="value" id="env">—</div></div><div class="card"><div class="label">标的</div><div class="value" id="symbol">—</div></div><div class="card"><div class="label">实际持仓</div><div class="value" id="position">—</div></div><div class="card"><div class="label">运行状态</div><div class="value" id="run">—</div></div>
<div class="card wide"><div class="label">账户</div><pre id="account">—</pre></div><div class="card wide"><div class="label">最新决策</div><pre id="decision">—</pre></div><div class="card full"><div class="label">订单</div><div id="orders">—</div></div><div class="card full"><div class="label">最近事件</div><pre id="events">—</pre></div></section></main>
<script>
const fmt=x=>JSON.stringify(x??null,null,2);async function api(path,options){const r=await fetch(path,options);const x=await r.json();if(!r.ok)throw Error(x.error||'request failed');return x}
async function refresh(){try{const s=await api('/api/status');health.textContent=s.quote_health;env.textContent=`${s.environment} / ${s.market}`;symbol.textContent=s.symbol;position.textContent=s.actual_quantity??'—';run.textContent=s.paused?'已暂停':'自动运行';account.textContent=fmt(s.account);decision.textContent=fmt(s.last_decision);events.textContent=fmt(s.events);orders.innerHTML=(s.orders||[]).map(o=>`<p>${o.channel_order_id} · ${o.side} ${o.quantity} @ ${o.limit_price} · ${o.status} <button class="danger" onclick="cancelOrder('${o.channel_order_id}')">撤单</button></p>`).join('')||'无订单'}catch(e){health.textContent='ERROR';events.textContent=e.message}}
async function post(path,body){try{await api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});await refresh()}catch(e){alert(e.message)}}
async function cancelOrder(id){if(!confirm(`确认准备撤销订单 ${id}？`))return;try{const x=await api('/api/cancel-token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({channel_order_id:id})});if(confirm(`再次确认撤销订单 ${id}`))await post('/api/cancel',{channel_order_id:id,token:x.token})}catch(e){alert(e.message)}}
refresh();setInterval(refresh,5000);
</script></body></html>"""


def create_server(
    operations: Operations, *, host: str = "127.0.0.1", port: int = 8765
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
                    token = operations.issue_cancel_token(str(body["channel_order_id"]))
                    result = {"token": token}
                elif self.path == "/api/cancel":
                    result = operations.confirm_cancel(
                        str(body["channel_order_id"]), str(body["token"])
                    )
                else:
                    self._json(404, {"error": "not found"})
                    return
                self._json(200, result)
            except Exception as exc:
                self._json(409, {"error": str(exc)})

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, int(port)), Handler)
