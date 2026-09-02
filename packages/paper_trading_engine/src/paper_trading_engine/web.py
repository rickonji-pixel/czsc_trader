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


DASHBOARD = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Paper Trading Engine</title>
<style>
:root{color-scheme:dark;--bg:#07111d;--card:#101e2d;--line:#294057;--text:#edf4fa;--muted:#91a7ba;--ok:#51d09a;--warn:#efbd5d;--bad:#ff747a}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 85% 0,#173a58,var(--bg) 42%);color:var(--text);font:14px/1.55 system-ui,sans-serif}main{max-width:1200px;margin:auto;padding:28px 20px 60px}header,.toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px}h1{margin:0;font-size:27px}.sub,.label{color:var(--muted)}.badge{border:1px solid var(--line);border-radius:99px;padding:6px 11px}.toolbar{justify-content:flex-start;margin:20px 0}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.card{background:rgba(16,30,45,.94);border:1px solid var(--line);border-radius:13px;padding:16px}.wide{grid-column:span 2}.full{grid-column:1/-1}.value{font-size:20px;font-weight:650;margin-top:4px}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.metric strong{display:block;font-size:18px;margin-top:4px}button{background:#193a54;color:var(--text);border:1px solid #3b5e79;border-radius:8px;padding:9px 13px;cursor:pointer}button:disabled{opacity:.45;cursor:wait}.danger{background:#52262d;border-color:#83414a}table{width:100%;border-collapse:collapse;margin-top:8px}th,td{padding:9px;border-bottom:1px solid var(--line);text-align:left}th{color:var(--muted);font-weight:500}.facts{display:grid;grid-template-columns:140px 1fr;gap:7px 12px;margin-top:10px}.facts span:nth-child(odd){color:var(--muted)}.event{padding:9px 0;border-bottom:1px solid var(--line)}details{margin-top:12px}pre{white-space:pre-wrap;max-height:300px;overflow:auto;color:#bdd1df}.toast{position:fixed;right:20px;bottom:20px;max-width:420px;padding:12px 16px;border-radius:9px;background:#17364b;border:1px solid #3d6580;opacity:0;transform:translateY(12px);transition:.2s;pointer-events:none}.toast.show{opacity:1;transform:none}.toast.error{background:#50242c;border-color:#8a3b47}@media(max-width:780px){.grid{grid-template-columns:1fr 1fr}.wide{grid-column:span 2}.metrics{grid-template-columns:1fr}.facts{grid-template-columns:1fr}}
</style></head><body><main><header><div><h1>Paper Trading Engine</h1><div class="sub">模拟交易观测与干预</div></div><span class="badge" id="health">正在连接</span></header>
<div class="toolbar"><button id="pauseButton" onclick="runAction('/api/pause','暂停成功')">暂停新订单</button><button id="resumeButton" onclick="runAction('/api/resume','恢复成功')">恢复运行</button></div>
<section class="grid"><div class="card"><div class="label">交易环境</div><div class="value" id="environment">—</div></div><div class="card"><div class="label">交易标的</div><div class="value" id="symbol">—</div></div><div class="card"><div class="label">实际持仓</div><div class="value" id="position">—</div></div><div class="card"><div class="label">运行状态</div><div class="value" id="runState">—</div></div>
<div class="card wide"><div class="label">账户概览</div><div class="metrics"><div class="metric"><span class="label">可用现金</span><strong id="cash">—</strong></div><div class="metric"><span class="label">总资产</span><strong id="assets">—</strong></div><div class="metric"><span class="label">冻结资金</span><strong id="frozen">—</strong></div></div></div>
<div class="card wide"><div class="label">最新策略决策</div><div class="facts"><span>决策编号</span><span id="decisionId">—</span><span>信号日期</span><span id="signalDate">—</span><span>有效交易日</span><span id="validSession">—</span><span>策略动作</span><span id="action">—</span><span>目标持仓</span><span id="target">—</span><span>执行参考价</span><span id="referencePrice">—</span></div></div>
<div class="card full"><div class="label">订单</div><table><thead><tr><th>订单号</th><th>方向</th><th>数量</th><th>限价</th><th>状态</th><th>累计成交</th><th>操作</th></tr></thead><tbody id="orderRows"></tbody></table></div>
<div class="card wide"><div class="label">告警</div><div id="alerts">暂无告警</div></div><div class="card wide"><div class="label">最近事件</div><div id="events">暂无事件</div></div>
<div class="card full"><details id="rawDetails"><summary>展开诊断数据</summary><pre id="diagnostics">—</pre></details></div></section></main><div class="toast" id="toast"></div>
<script>
const labels={SIMULATE:'模拟交易',CN:'中国市场',OK:'行情正常',DEGRADED_QUOTE:'行情降级',UNKNOWN:'状态未知',WAIT:'等待',HOLD:'持有',BUY:'买入',SELL:'卖出',SUBMITTING:'提交中',SUBMITTED:'已提交',SUBMIT_FAILED:'提交失败',FILLED_PART:'部分成交',FILLED_ALL:'全部成交',CANCELLED_PART:'部分撤销',CANCELLED_ALL:'全部撤销',FAILED:'失败',DISABLED:'已禁用',DELETED:'已删除',FILL_CANCELLED:'成交已撤销',ACTIVE_ORDER_BLOCKS_SUBMISSION:'存在活动订单，暂不提交',DECISION_NOT_VALID_TODAY:'当前不是决策有效交易日',OUTSIDE_SUBMISSION_WINDOW:'当前不在自动发单时段',ORDER_INTENT_CREATED:'已创建订单意图',ORDER_SUBMITTED:'订单已提交',FILL_INCREMENT:'收到新增成交',PAUSED:'已暂停自动下单',RESUMED:'已恢复自动运行',CANCEL_TOKEN_ISSUED:'已发出撤单确认令牌',CANCEL_REQUESTED:'已提交撤单请求',DATA_PUBLISHED:'完整收盘数据已发布',DATA_PUBLICATION_FAILED:'完整收盘数据发布失败',SCHEDULER_CYCLE_FAILED:'调度周期执行失败'};
const cn=x=>labels[x]||x||'—', money=x=>x==null?'—':Number(x).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2}), qty=x=>x==null?'—':Number(x).toLocaleString('zh-CN');
async function api(path,options){const r=await fetch(path,options),x=await r.json();if(!r.ok)throw Error(x.error||'请求失败');return x}
function set(id,value){document.getElementById(id).textContent=value}
function showToast(message,error=false){const el=document.getElementById('toast');el.textContent=message;el.className='toast show'+(error?' error':'');clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>el.className='toast',3000)}
function renderList(id,items,formatter){const box=document.getElementById(id);box.replaceChildren();if(!items?.length){box.textContent=id==='alerts'?'暂无告警':'暂无事件';return}items.forEach(item=>{const row=document.createElement('div');row.className='event';row.textContent=formatter(item);box.appendChild(row)})}
function renderOrders(orders){const body=document.getElementById('orderRows');body.replaceChildren();if(!orders?.length){const row=body.insertRow();const cell=row.insertCell();cell.colSpan=7;cell.textContent='暂无订单';return}orders.forEach(o=>{const row=body.insertRow();[o.channel_order_id,cn(o.side),qty(o.quantity),money(o.limit_price),cn(o.status),qty(o.cumulative_filled_quantity)].forEach(v=>{const c=row.insertCell();c.textContent=v});const c=row.insertCell(),b=document.createElement('button');b.className='danger';b.textContent='撤单';b.onclick=()=>cancelOrder(o.channel_order_id);c.appendChild(b)})}
function render(s){const a=s.account||{},d=s.last_decision||{};set('health',cn(s.quote_health));set('environment',`${cn(s.environment)} / ${cn(s.market)}`);set('symbol',s.symbol);set('position',qty(s.actual_quantity));set('runState',s.paused?'已暂停':'自动运行');set('cash',money(a.cash));set('assets',money(a.total_assets));set('frozen',money(a.frozen_cash));set('decisionId',d.decision_id);set('signalDate',d.signal_date);set('validSession',d.valid_session);set('action',cn(d.action));set('target',qty(d.target_quantity));set('referencePrice',money(d.execution_reference_price));renderOrders(s.orders);renderList('alerts',s.alerts,x=>cn(x));renderList('events',s.events,e=>`${e.created_at} · ${cn(e.event_type)}`);set('diagnostics',JSON.stringify(s,null,2));document.getElementById('pauseButton').disabled=!!s.paused;document.getElementById('resumeButton').disabled=!s.paused}
async function refresh(){try{render(await api('/api/status'))}catch(e){showToast(`状态刷新失败：${e.message}`,true)}}
async function runAction(path,success){const buttons=document.querySelectorAll('.toolbar button');buttons.forEach(b=>b.disabled=true);try{const state=await api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});render(state);showToast(success)}catch(e){showToast(`操作失败：${e.message}`,true)}finally{setTimeout(refresh,100)}}
async function cancelOrder(id){if(!confirm(`确认准备撤销订单 ${id}？`))return;try{const x=await api('/api/cancel-token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({channel_order_id:id})});if(confirm(`再次确认撤销订单 ${id}`)){const state=await api('/api/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({channel_order_id:id,token:x.token})});render(state);showToast('撤单请求已提交')}}catch(e){showToast(`撤单失败：${e.message}`,true)}}
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
                    result = {"token": operations.issue_cancel_token(str(body["channel_order_id"]))}
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
