export function parseRoute(pathname) {
  const account = pathname.match(/^\/accounts\/([^/]+)$/);
  if (account) return {page: 'account', accountId: decodeURIComponent(account[1])};
  if (pathname === '/channels/futu') return {page: 'channel', channel: 'futu'};
  if (pathname === '/comparison') return {page: 'comparison'};
  if (pathname === '/audit-events') return {page: 'audit'};
  return {page: 'account', accountId: null};
}

export class ScopedLoader {
  constructor(){this.sequence=0;this.scope=null;this.controller=null;}
  begin(scope){if(this.controller)this.controller.abort();this.controller=new AbortController();this.scope=scope;return {sequence:++this.sequence,scope,signal:this.controller.signal};}
  accept(request,scope){return request.sequence===this.sequence&&request.scope===scope;}
}

export const actionsForRoute = route => route.page==='channel'?['pause','resume','cancel']:route.page==='account'?['pause','resume']:[];
export const comparisonQuery = ids => ids.map(id=>`account_id=${encodeURIComponent(id)}`).join('&');
export const auditQuery = filters => {
  const query=new URLSearchParams();
  Object.entries(filters||{}).forEach(([key,value])=>{if(value!=null&&value!=='')query.set(key,value);});
  return query.toString();
};
export const auditCategoryLabel = value => ({STRATEGY:'策略事件',TRADING:'交易事件',SYSTEM:'系统事件',OTHER:'其他事件'}[value]||value||'未分类');
export const auditEventLabel = value => ({
  MARKET_DATA_PUBLICATION_REQUESTED:'请求发布数据',MARKET_DATA_PUBLISHED:'发布数据成功',MARKET_DATA_PUBLICATION_FAILED:'发布数据失败',DECISION_GENERATED:'生成决策',DECISION_GENERATION_FAILED:'生成决策失败',SIGNAL_TRIGGERED:'触发信号',SIGNAL_CLEARED:'信号解除',DECISION_EXPIRED:'决策过期',CHANNEL_STRATEGY_BOUND:'历史关系 · 渠道曾直接绑定策略',ACCOUNT_STRATEGY_BOUND:'虚拟账户绑定策略',ACCOUNT_CHANNEL_BOUND:'虚拟账户绑定渠道',CHANNEL_RECONCILIATION_FAILED:'渠道对账失败',CHANNEL_RECONCILIATION_RECOVERED:'渠道对账恢复',
  ORDER_INTENT_CREATED:'创建订单意图',ORDER_INTENT_RECOVERED:'恢复订单意图',ORDER_SUBMISSION_BLOCKED:'订单提交受阻',ORDER_SUBMITTED:'订单已提交',ORDER_SUBMISSION_FAILED:'订单提交失败',CANCEL_REQUESTED:'请求撤单',CANCEL_SUCCEEDED:'撤单成功',CANCEL_FAILED:'撤单失败',ORDER_PARTIALLY_FILLED:'订单部分成交',ORDER_FILLED:'订单成交',ORDER_TERMINATED:'订单终止',
  SERVICE_STARTED:'服务启动',SERVICE_STOPPED:'服务停止',RESTART_REQUESTED:'请求重启',ACCOUNT_PAUSED:'账户暂停',ACCOUNT_RESUMED:'账户恢复',EXTERNAL_CALL_SUCCEEDED:'外部接口调用成功',EXTERNAL_CALL_FAILED:'外部接口调用失败',DEPENDENCY_DEGRADED:'外部依赖降级',DEPENDENCY_RECOVERED:'外部依赖恢复',SCHEDULER_OPERATION_FAILED:'调度任务失败',SCHEDULER_OPERATION_RECOVERED:'调度任务恢复',SCHEDULER_CYCLE_FAILED:'调度周期失败',VIRTUAL_ACCOUNT_FAILED:'虚拟账户运行失败',ACCOUNT_CHART_GENERATION_FAILED:'前瞻观察图生成失败',ACCOUNT_CHART_RECOVERED:'前瞻观察图恢复',LEGACY_EVENT:'历史事件',UNCLASSIFIED_EVENT:'未分类事件',
}[value]||value||'未知事件');
export const auditSeverityLabel = value => ({INFO:'信息',WARNING:'警告',ERROR:'错误',CRITICAL:'严重'}[value]||value||'未知');
export const auditOutcomeLabel = value => ({SUCCESS:'成功',FAILURE:'失败',REJECTED:'已拒绝',SKIPPED:'已跳过',UNKNOWN:'未知'}[value]||value||'未知');
export const auditScopeLabel = (event,accounts=[]) => {
  const account=accounts.find(item=>item.account_id===event?.account_id);
  const accountLabel=event?.account_id?`${account?.name||event.account_id}（${event.account_id}）`:null;
  if(accountLabel)return `虚拟账户 · ${accountLabel}`;
  if(event?.event_type==='DECISION_GENERATED'&&event?.channel==='futu')return '历史记录 · 虚拟账户未记录';
  if(event?.channel==='futu')return 'Futu模拟渠道';
  return '历史记录 · 作用域未记录';
};
export const snapshotFingerprint = value => JSON.stringify(value,(key,item)=>key==='as_of'?undefined:item);
export const chartShouldReload = (previous,status) => !previous||previous.account_id!==status?.scope?.account_id||previous.fingerprint!==status?.fingerprint;
export const systemEventLabel = value => ({
  DATA_PUBLICATION_FAILED:'发布数据失败',
  DATA_PUBLISHED:'数据发布成功',
  SCHEDULER_OPERATION_FAILED:'调度任务失败',
  SCHEDULER_OPERATION_RECOVERED:'调度任务恢复',
  SCHEDULER_CYCLE_FAILED:'调度周期失败',
}[value]||auditEventLabel(value));
export const systemAlertCount = value => (value?.alerts?.length||0)+(value?.scheduler_failures?.length||0);
export const chooseAccountId = (requested, accounts, defaultAccountId) => accounts.some(item=>item.account_id===requested)?requested:defaultAccountId;
export const navigationOptions = renderedScope => ({showLoading:renderedScope==null,forceRender:false});
export const channelOrderAccountLabel = order => order?.account_id||order?.virtual_account_id||'历史未记录';
export const displayFillId = value => value?`FIL-${String(value).split('-',1)[0].toUpperCase()}`:'—';
export function formatBeijingTime(value){
  if(!value)return '—';
  const date=new Date(value);if(Number.isNaN(date.getTime()))return String(value);
  const parts=Object.fromEntries(new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(date).filter(item=>item.type!=='literal').map(item=>[item.type,item.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}

const esc = value => String(value??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = value => value==null?'—':Number(value).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
const pct = value => value==null?'—':`${(Number(value)*100).toFixed(2)}%`;
const shortHash = value => value?String(value).slice(0,10):'—';
const actionLabel = value => ({BUY:'买入',SELL:'卖出',WAIT:'等待'}[value]||value||'等待');
const statusLabel = value => ({READY:'就绪',RUNNING:'运行中',OK:'正常',BLOCKED:'阻塞',UNAVAILABLE:'不可用',CHANNEL_RECONCILIATION_BLOCKED:'渠道对账阻塞',SUBMITTED:'已提交',SUBMITTING:'提交中',FILLED_PART:'部分成交',FILLED_ALL:'全部成交',CANCELLED_ALL:'已撤销',PENDING:'待执行'}[value]||value||'—');
const state = {accounts:[],loader:new ScopedLoader(),lastSuccess:null,renderedScope:null,fingerprint:null,system:null,systemFingerprint:null,chart:null};

async function getJson(url, options={}) {
  const response=await fetch(url,options);let payload={};
  try{payload=await response.json();}catch{payload={error:`HTTP ${response.status}`};}
  if(!response.ok)throw new Error(payload.error||`HTTP ${response.status}`);return payload;
}
function post(url,body={}){return getJson(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}
function toast(message){const el=document.querySelector('#toast');el.textContent=message;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),2200);}
function setNav(page){document.querySelectorAll('[data-nav]').forEach(el=>el.classList.toggle('active',el.dataset.nav===page));}

function renderEventDetails(payload){return `<details><summary>查看详情</summary><pre>${esc(JSON.stringify(payload||{},null,2))}</pre></details>`;}
function renderSystemEvents(s){
  const alerts=[...(s.alerts||[]).map(code=>({label:statusLabel(code),detail:{code}})),...(s.scheduler_failures||[]).map(item=>({label:'失败任务',detail:item}))];
  const active=alerts.length?alerts.map(item=>`<div class="event active-event"><strong>${esc(item.label)}</strong>${renderEventDetails(item.detail)}</div>`).join(''):'<div class="empty">当前没有活动告警</div>';
  const history=(s.events||[]).length?(s.events||[]).map(item=>`<div class="event"><div class="event-title"><strong>${esc(systemEventLabel(item.event_type))}</strong><time>${esc(formatBeijingTime(item.created_at))}</time></div>${renderEventDetails(item.payload)}</div>`).join(''):'<div class="empty">暂无系统事件</div>';
  document.querySelector('#systemEventsContent').innerHTML=`<section><h3>活动告警</h3>${active}</section><section><h3>最近事件</h3>${history}</section>`;
}
function openSystemEvents(){if(state.system)renderSystemEvents(state.system);const dialog=document.querySelector('#systemEventsDialog');if(dialog.showModal)dialog.showModal();else dialog.setAttribute('open','');}
async function refreshSystem(){try{const s=await getJson('/api/system/status');const fingerprint=snapshotFingerprint(s);const changed=fingerprint!==state.systemFingerprint;state.system=s;state.systemFingerprint=fingerprint;const count=systemAlertCount(s);const status=document.querySelector('#systemStatus');status.textContent=`PTE ${statusLabel(s.runtime)} · Futu ${statusLabel(s.futu_connection)} · 全局告警 ${count} · 查看系统事件`;status.classList.toggle('warning',count>0);if(changed&&document.querySelector('#systemEventsDialog')?.open)renderSystemEvents(s);}catch(error){document.querySelector('#systemStatus').textContent=`系统状态不可用 · ${error.message}`;}}
function metric(label,value){return `<div class="panel metric"><label>${label}</label><strong>${value}</strong></div>`;}
function rowsTable(columns,rows){if(!rows?.length)return '<div class="empty">暂无记录</div>';return `<div class="table-wrap"><table><thead><tr>${columns.map(c=>`<th>${c[0]}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${columns.map(c=>{const title=c[2]?.(row);return `<td${title?` title="${esc(title)}"`:''}>${esc(c[1](row))}</td>`;}).join('')}</tr>`).join('')}</tbody></table></div>`;}
function alerts(items){return (items||[]).map(item=>`<div class="alert">${esc(item.message||item)}</div>`).join('');}

export const ACCOUNT_REFRESH_SECTIONS=['sidebar','hero','alerts','metrics','decision','orders','fills','performance'];
function accountMarkup(snapshot){
  const a=snapshot.account,m=snapshot.metrics||{},d=snapshot.decision||{},scope=snapshot.scope,orders=snapshot.orders||[],fills=snapshot.fills||[];
  return `<div class="layout" data-account-page data-account-id="${esc(scope.account_id)}"><aside class="sidebar" data-account-section="sidebar"><h2>虚拟账户</h2>${state.accounts.map(item=>`<button class="account-card ${item.account_id===scope.account_id?'active':''}" data-account="${esc(item.account_id)}"><strong>${esc(item.name)}</strong><small>${esc(item.account_id)} · ${esc(item.release_id)}</small><small>${item.paused?'已暂停':statusLabel(item.health)} · 资产 ${money(item.total_assets)}</small></button>`).join('')}</aside><div class="workspace"><section class="panel hero" data-account-section="hero"><div><div class="eyebrow">虚拟账户 · ${esc(scope.account_id)}</div><h1>${esc(a.name)}</h1><div class="subtle">${esc(a.strategy_name_snapshot)} · ${esc(scope.release_id)} · ${shortHash(scope.release_hash)} · ${esc(a.qualification_snapshot)}</div><div class="subtle">执行渠道 Futu · 选择截止 ${esc(a.selection_data_cutoff)} · 观察起点 ${esc(a.observation_start)} · 最近同步 ${esc(formatBeijingTime(snapshot.as_of))}</div></div><button id="accountSwitch" class="switch ${a.paused?'paused':''}" role="switch" aria-checked="${!a.paused}">${a.paused?'已暂停 · 点击恢复':'自动运行 · 点击暂停'}</button></section><div data-account-section="alerts">${alerts(snapshot.alerts)}</div><div class="grid" data-account-section="metrics">${metric('总资产',money(a.total_assets))}${metric('可用现金',money(a.cash))}${metric('持仓',`${Number(a.quantity||0).toLocaleString()} 股`)}${metric('累计收益',pct(m.total_return))}</div><section class="panel section" data-account-section="decision"><div class="section-head"><h2>最新决策</h2><span class="badge">${esc(scope.account_id)}</span></div><div class="decision"><div class="decision-action">${esc(actionLabel(d.action))}</div><div class="kv"><div><span>决策ID</span>${esc(d.decision_id)}</div><div><span>信号日期</span>${esc(d.signal_date)}</div><div><span>有效交易日</span>${esc(d.valid_session)}</div><div><span>目标持仓</span>${esc(d.target_quantity)}</div><div><span>执行参考价</span>${esc(d.execution_reference_price)}</div><div><span>未下单原因</span>${esc((snapshot.alerts||[])[0]?.message||'—')}</div></div></div></section><section class="panel section account-chart" data-account-section="chart"><div class="section-head"><h2>前瞻观察</h2><span class="badge">${esc(scope.release_id)} · 选择截止 ${esc(a.selection_data_cutoff)}</span></div><div class="chart-governance">截止线左侧仅作行情背景；右侧为该策略版本的前瞻观察记录。</div><div id="accountChartMessage" class="chart-message" hidden></div><div id="accountChartFrameHost" class="chart-frame-host"><div class="loading">正在加载前瞻观察图…</div></div></section><section class="panel section" data-account-section="orders"><div class="section-head"><h2>订单</h2><span class="badge">账户 ${esc(scope.account_id)}</span></div>${rowsTable([['账户',r=>r.account_id],['Futu订单',r=>r.channel_order_id],['方向',r=>r.side],['数量',r=>r.quantity],['限价',r=>r.limit_price],['状态',r=>statusLabel(r.status)]],orders)}</section><section class="panel section" data-account-section="fills"><div class="section-head"><h2>成交</h2><span class="badge">账户 ${esc(scope.account_id)}</span></div>${rowsTable([['账户',r=>r.account_id],['成交ID',r=>displayFillId(r.fill_id),r=>r.fill_id],['时间',r=>formatBeijingTime(r.occurred_at)],['方向',r=>r.side],['数量',r=>r.quantity],['价格',r=>r.price]],fills)}</section><section class="panel section" data-account-section="performance"><div class="section-head"><h2>绩效与审计</h2><span class="badge">共同口径</span></div><div class="grid">${metric('最大回撤',pct(m.maximum_drawdown))}${metric('卡玛比率',m.calmar_ratio==null?'不可用':Number(m.calmar_ratio).toFixed(3))}${metric('盈亏比',m.win_loss_ratio==null?'不可用':Number(m.win_loss_ratio).toFixed(3))}${metric('已闭合交易',esc(m.closed_trades||0))}</div>${rowsTable([['时间',r=>formatBeijingTime(r.occurred_at||r.created_at)],['事件',r=>auditEventLabel(r.event_type)],['内容',r=>JSON.stringify(r.details||r.payload)]],snapshot.events)}</section></div></div>`;
}
function replaceAccountSections(markup,accountId){
  const current=document.querySelector('[data-account-page]');
  if(!current||current.dataset.accountId!==accountId)return false;
  const staging=document.createElement('div');staging.innerHTML=markup;
  const next=staging.firstElementChild;
  const pairs=ACCOUNT_REFRESH_SECTIONS.map(key=>[
    current.querySelector(`[data-account-section="${key}"]`),
    next.querySelector(`[data-account-section="${key}"]`),
  ]);
  if(pairs.some(([existing,fresh])=>!existing||!fresh))return false;
  pairs.forEach(([existing,fresh])=>existing.replaceWith(fresh));
  return true;
}
function renderAccount(snapshot){
  const markup=accountMarkup(snapshot),scope=snapshot.scope,a=snapshot.account;
  if(!replaceAccountSections(markup,scope.account_id))document.querySelector('#app').innerHTML=markup;
  bindAccountEvents(scope.account_id,a.paused);
}
function applyAccountChart(status){
  const host=document.querySelector('#accountChartFrameHost'),message=document.querySelector('#accountChartMessage');
  if(!host||status?.scope?.account_id!==parseRoute(location.pathname).accountId)return;
  message.textContent=status.message||'';message.hidden=!status.message;
  if(status.status==='UNAVAILABLE'||!status.chart_url){
    host.innerHTML=`<div class="error"><strong>前瞻观察图不可用</strong><div>${esc(status.message)}</div><button id="retryAccountChart" class="button" type="button">重新加载图表</button></div>`;
    document.querySelector('#retryAccountChart').onclick=()=>loadRoute({showLoading:false});
    state.chart={account_id:status.scope.account_id,fingerprint:null};return;
  }
  const current={account_id:status.scope.account_id,fingerprint:status.fingerprint};
  if(chartShouldReload(state.chart,status)||!document.querySelector('#accountChartFrame')){
    host.innerHTML=`<iframe id="accountChartFrame" data-account-id="${esc(status.scope.account_id)}" title="${esc(status.scope.release_id)}前瞻观察图" src="${esc(status.chart_url)}" scrolling="no"></iframe>`;
  }
  state.chart=current;
}
function bindAccountEvents(accountId,paused){document.querySelectorAll('[data-account]').forEach(el=>el.onclick=()=>navigate(`/accounts/${encodeURIComponent(el.dataset.account)}`));document.querySelector('#accountSwitch').onclick=async()=>{try{await post(`/api/virtual-accounts/${encodeURIComponent(accountId)}/${paused?'resume':'pause'}`);toast(paused?'账户已恢复':'账户已暂停');await loadRoute();}catch(e){toast(e.message);}};}

function renderChannel(s){document.querySelector('#app').innerHTML=`<div class="workspace"><section class="panel hero"><div><div class="eyebrow">执行渠道 · FUTU</div><h1>Futu 模拟渠道</h1><div class="subtle">底层模拟账户 · 对账 ${statusLabel(s.reconciliation_status)} · 最近同步 ${esc(formatBeijingTime(s.as_of))}</div></div><button id="channelSwitch" class="switch ${s.paused?'paused':''}" role="switch" aria-checked="${!s.paused}">${s.paused?'渠道已暂停 · 点击恢复':'渠道自动运行 · 点击暂停'}</button></section>${alerts((s.alerts||[]).map(x=>({message:x})))}<div class="grid">${metric('可用现金',money(s.account?.cash))}${metric('总资产',money(s.account?.total_assets))}${metric('已分配资金',money(s.allocated_capital))}${metric('未分配资金',money(s.unallocated_capital))}</div><section class="panel section"><div class="section-head"><h2>承载账户</h2><span class="badge">${(s.accounts||[]).length} 个账户</span></div>${rowsTable([['虚拟账户',r=>r.name],['账户ID',r=>r.account_id],['策略版本',r=>r.release_id],['分配资金',r=>money(r.initial_cash)],['可用现金',r=>money(r.cash)],['持仓',r=>r.quantity],['状态',r=>r.paused?'已暂停':statusLabel(r.status)]],s.accounts)}</section><section id="channelOrders" class="panel section"><div class="section-head"><h2>订单</h2><span class="badge">逐笔归属</span></div>${rowsTable([['时间',r=>formatBeijingTime(r.created_at)],['Futu订单',r=>r.channel_order_id],['虚拟账户',r=>channelOrderAccountLabel(r)],['决策ID',r=>r.decision_id||'历史未记录'],['方向',r=>r.side],['数量',r=>r.quantity],['价格',r=>r.limit_price],['状态',r=>statusLabel(r.status)],['操作',r=>['SUBMITTED','PARTIAL_FILLED'].includes(r.status)?'二次确认撤单':'—']],s.orders)}</section><section class="panel section"><div class="section-head"><h2>成交</h2><span class="badge">Futu回报</span></div>${rowsTable([['时间',r=>formatBeijingTime(r.occurred_at)],['虚拟账户',r=>channelOrderAccountLabel(r)],['成交ID',r=>displayFillId(r.fill_id),r=>r.fill_id],['Futu订单',r=>r.order_id],['方向',r=>r.side],['数量',r=>r.quantity],['价格',r=>r.price],['费用',r=>r.fee]],s.fills)}</section></div>`;document.querySelector('#channelSwitch').onclick=async()=>{try{await post(`/api/channels/futu/${s.paused?'resume':'pause'}`);toast(s.paused?'渠道已恢复':'渠道已暂停');await loadRoute({showLoading:false});}catch(e){toast(e.message);}};document.querySelectorAll('#channelOrders tbody tr').forEach((tr,i)=>{const o=s.orders?.[i];if(o&&['SUBMITTED','PARTIAL_FILLED'].includes(o.status))tr.onclick=()=>cancelOrder(o);});}
async function cancelOrder(order){if(!confirm(`确认申请撤销Futu订单？\n订单 ${order.channel_order_id}\n${order.side} ${order.quantity}股\n虚拟账户 ${channelOrderAccountLabel(order)}`))return;try{const body={account_id:order.account_id,channel_order_id:order.channel_order_id};const {token}=await post('/api/channels/futu/cancel-token',body);if(!confirm('撤单令牌已签发，有效期2分钟。再次确认撤单。'))return;await post('/api/channels/futu/cancel',{...body,token});toast('撤单请求已提交');await loadRoute({showLoading:false});}catch(e){toast(e.message);}}

function renderComparison(s){document.querySelector('#app').innerHTML=`<section class="panel hero"><div><div class="eyebrow">只读评估</div><h1>虚拟账户比较</h1><div class="subtle">共同观察区间 ${esc(s.common_window?.start)} 至 ${esc(s.common_window?.end)}</div></div></section><section class="panel section"><div class="section-head"><h2>参与账户</h2><span class="badge">无交易干预</span></div><div class="comparison-select">${state.accounts.map(a=>`<label><input type="checkbox" value="${esc(a.account_id)}" ${s.accounts.some(x=>x.account_id===a.account_id)?'checked':''}> ${esc(a.name)} · ${esc(a.release_id)}</label>`).join('')}</div></section><section class="panel section"><div class="section-head"><h2>核心指标</h2><span class="badge">OPC优先级</span></div>${rowsTable([['账户',r=>r.account_id],['策略发布',r=>r.release_id],['累计收益',r=>pct(r.metrics.total_return)],['最大回撤',r=>pct(r.metrics.maximum_drawdown)],['卡玛比率',r=>r.metrics.calmar_ratio==null?'不可用':Number(r.metrics.calmar_ratio).toFixed(3)],['盈亏比',r=>r.metrics.win_loss_ratio==null?'不可用':Number(r.metrics.win_loss_ratio).toFixed(3)]],s.accounts)}</section>`;document.querySelectorAll('.comparison-select input').forEach(el=>el.onchange=()=>{const ids=[...document.querySelectorAll('.comparison-select input:checked')].map(x=>x.value);navigate(`/comparison${ids.length?'?'+comparisonQuery(ids):''}`);});}

function renderAudit(s){
  const selected=new URLSearchParams(location.search),categories=['STRATEGY','TRADING','SYSTEM','OTHER'];
  const cards=categories.map(category=>`<button class="panel metric audit-category" data-category="${category}"><label>${auditCategoryLabel(category)}</label><strong>${(s.events||[]).filter(event=>event.category===category).length}</strong></button>`).join('');
  const options=categories.map(category=>`<option value="${category}" ${selected.get('category')===category?'selected':''}>${auditCategoryLabel(category)}</option>`).join('');
  const events=(s.events||[]).length?(s.events||[]).map(event=>`<article class="event audit-event"><div class="event-title"><div><span class="badge">${esc(auditCategoryLabel(event.category))}</span> <strong>${esc(auditEventLabel(event.event_type))}</strong></div><time>${esc(formatBeijingTime(event.occurred_at))}</time></div><div class="audit-scope"><span>归属</span><strong>${esc(auditScopeLabel(event,state.accounts))}</strong></div><div class="audit-meta"><span>级别 ${esc(auditSeverityLabel(event.severity))}</span><span>结果 ${esc(auditOutcomeLabel(event.outcome))}</span>${event.strategy_id?`<span>策略 ${esc(event.strategy_id)}${event.strategy_version?` · 版本 ${esc(event.strategy_version)}`:''}</span>`:''}${event.channel==='futu'?'<span>执行渠道 Futu模拟渠道</span>':''}</div>${event.correlation_id?`<button class="link-button" data-correlation="${esc(event.correlation_id)}">关联 ${esc(event.correlation_id)}</button>`:''}${renderEventDetails(event.details)}</article>`).join(''):'<div class="empty">当前筛选条件下暂无事件</div>';
  document.querySelector('#app').innerHTML=`<section class="panel hero"><div><div class="eyebrow">PTE · 只读审计账本</div><h1>运行事件</h1><div class="subtle">事件以 UTC 持久化，页面统一显示北京时间 · 最近同步 ${esc(formatBeijingTime(s.as_of))}</div></div></section><div class="grid audit-summary">${cards}</div><section class="panel section"><form id="auditFilters" class="audit-filters"><label>类别<select name="category"><option value="">全部类别</option>${options}</select></label><label>账户<input name="account_id" value="${esc(selected.get('account_id')||'')}" placeholder="账户ID"></label><label>策略<input name="strategy_id" value="${esc(selected.get('strategy_id')||'')}" placeholder="策略ID"></label><label>渠道<input name="channel" value="${esc(selected.get('channel')||'')}" placeholder="如 futu"></label><label>关联ID<input name="correlation_id" value="${esc(selected.get('correlation_id')||'')}" placeholder="决策或发布链路"></label><button class="button" type="submit">筛选</button><button class="button secondary" id="clearAuditFilters" type="button">清空</button></form></section><section class="panel section"><div class="section-head"><h2>事件明细</h2><span class="badge">${(s.events||[]).length} 条</span></div><div class="audit-list">${events}</div>${s.next_before_id?'<button id="moreAudit" class="button secondary" type="button">查看更早事件</button>':''}</section>`;
  document.querySelectorAll('[data-category]').forEach(el=>el.onclick=()=>{const params=new URLSearchParams(location.search);params.set('category',el.dataset.category);navigate(`/audit-events?${params}`);});
  document.querySelectorAll('[data-correlation]').forEach(el=>el.onclick=()=>navigate(`/audit-events?${auditQuery({correlation_id:el.dataset.correlation})}`));
  document.querySelector('#auditFilters').onsubmit=event=>{event.preventDefault();const query=auditQuery(Object.fromEntries(new FormData(event.currentTarget)));navigate(`/audit-events${query?'?'+query:''}`);};
  document.querySelector('#clearAuditFilters').onclick=()=>navigate('/audit-events');
  const more=document.querySelector('#moreAudit');if(more)more.onclick=()=>{const params=new URLSearchParams(location.search);params.set('before_id',s.next_before_id);navigate(`/audit-events?${params}`);};
}

async function loadAccounts(options={}){const payload=await getJson('/api/virtual-accounts',options);state.accounts=payload.accounts||[];return payload;}
async function loadRoute({showLoading=true,forceRender=false}={}){
  const route=parseRoute(location.pathname);setNav(route.page);
  let scope=route.page==='account'?route.accountId:`${route.page}${location.search}`;
  const request=state.loader.begin(scope);
  try{
    if(showLoading)document.querySelector('#app').innerHTML='<div class="loading">正在加载当前作用域…</div>';
    const accountIndex=await loadAccounts({signal:request.signal});
    if(route.page==='account'){
      const requested=route.accountId||localStorage.getItem('pte.lastAccountId');
      const id=chooseAccountId(requested,state.accounts,accountIndex.default_account_id);
      if(!id)throw new Error('尚无虚拟账户');
      if(route.accountId!==id){navigate(`/accounts/${encodeURIComponent(id)}`,true);return;}
    }
    scope=route.page==='account'?route.accountId:`${route.page}${location.search}`;
    let snapshot,chartPromise=null;
    if(route.page==='account'){
      chartPromise=getJson(`/api/virtual-accounts/${encodeURIComponent(route.accountId)}/chart`,{signal:request.signal}).catch(error=>({scope:{account_id:route.accountId},status:'UNAVAILABLE',chart_url:null,fingerprint:null,message:error.message}));
      snapshot=await getJson(`/api/virtual-accounts/${encodeURIComponent(route.accountId)}/snapshot`,{signal:request.signal});
    }
    else if(route.page==='channel')snapshot=await getJson('/api/channels/futu/snapshot',{signal:request.signal});
    else if(route.page==='comparison'){const ids=new URLSearchParams(location.search).getAll('account_id');snapshot=await getJson(`/api/comparison${ids.length?'?'+comparisonQuery(ids):''}`,{signal:request.signal});}
    else snapshot=await getJson(`/api/audit-events${location.search}`,{signal:request.signal});
    if(!state.loader.accept(request,scope))return;
    if(route.page==='account'&&snapshot.scope.account_id!==route.accountId)throw new Error('服务端账户作用域不一致');
    const fingerprint=snapshotFingerprint({accounts:state.accounts,snapshot});
    if(forceRender||scope!==state.renderedScope||fingerprint!==state.fingerprint){
      if(route.page==='account'){localStorage.setItem('pte.lastAccountId',route.accountId);renderAccount(snapshot);}
      else if(route.page==='channel')renderChannel(snapshot);else if(route.page==='comparison')renderComparison(snapshot);else renderAudit(snapshot);
      state.renderedScope=scope;state.fingerprint=fingerprint;
    }
    if(route.page==='account'){
      const chartStatus=await chartPromise;
      if(!state.loader.accept(request,scope)||chartStatus.scope.account_id!==route.accountId)return;
      applyAccountChart(chartStatus);
    }
    state.lastSuccess=new Date();
    const warning=document.querySelector('#pollWarning');warning.hidden=true;warning.textContent='';
  }catch(error){
    if(error.name==='AbortError')return;
    const message=`刷新失败：${error.message} · 继续显示最后成功数据`;
    const warning=document.querySelector('#pollWarning');warning.hidden=false;warning.textContent=message;
    if(showLoading||state.renderedScope!==scope)document.querySelector('#app').innerHTML=`<div class="error"><strong>当前作用域加载失败</strong><div>${esc(error.message)}</div><div class="subtle">最后成功 ${state.lastSuccess?state.lastSuccess.toLocaleString():'无'}</div></div>`;
  }
}
function navigate(url,replace=false){if(url!==`${location.pathname}${location.search}`)history[replace?'replaceState':'pushState']({},'',url);loadRoute(navigationOptions(state.renderedScope));}
function boot(){document.querySelectorAll('a[href^="/"]').forEach(a=>a.onclick=e=>{e.preventDefault();navigate(a.getAttribute('href'));});document.querySelector('#systemStatus').onclick=openSystemEvents;document.querySelector('#closeSystemEvents').onclick=()=>document.querySelector('#systemEventsDialog').close();window.addEventListener('popstate',()=>loadRoute(navigationOptions(state.renderedScope)));refreshSystem();loadRoute({showLoading:true,forceRender:true});setInterval(refreshSystem,10000);setInterval(()=>loadRoute({showLoading:false}),5000);}
if(typeof document!=='undefined')boot();
