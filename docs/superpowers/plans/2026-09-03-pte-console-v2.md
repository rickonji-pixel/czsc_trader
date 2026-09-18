# PTE Console v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付以虚拟账户、Futu渠道和账户比较为三个明确作用域的PTE控制台，并通过显式策略绑定杜绝跨账户误读与误操作。

**Architecture:** 后端使用资源级HTTP接口返回带`scope`的完整快照，前端以URL作为当前作用域的唯一事实来源。Futu策略绑定持久化到现有SQLite `settings`表并由CLI治理，页面仅展示；静态页面拆为包内HTML、CSS和JavaScript，继续使用标准库HTTP服务。

**Tech Stack:** Python 3.11、SQLite、标准库`http.server`、原生HTML/CSS/JavaScript、pytest、Node.js语法检查。

**Spec:** `docs/superpowers/specs/2026-09-03-pte-console-v2-design.md`

## Global Constraints

- 不增加实盘交易能力，Futu调用继续硬锁`TrdEnv.SIMULATE`。
- 不改变策略信号、执行定价和虚拟成交规则。
- 一个虚拟账户永久绑定一个`strategy_id + version + release_hash`。
- 一个决策、模型订单和模型成交只能属于一个虚拟账户。
- Futu渠道任一时刻只绑定一个正式策略版本，冻结新策略不自动改变该绑定。
- 虚拟账户页面不提供Futu撤单，比较页面不提供任何干预操作。
- 采用原生HTML/CSS/JavaScript，不增加Node运行时、前端框架或构建链。
- `/api/status`在兼容期保留，仅供watchdog和旧客户端使用。

---

### Task 1: Futu渠道策略绑定领域模型与CLI

**Files:**
- Create: `packages/paper_trading_engine/src/paper_trading_engine/channel_binding.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/cli.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/engine.py`
- Test: `packages/paper_trading_engine/tests/test_channel_binding.py`
- Test: `packages/paper_trading_engine/tests/test_cli.py`

**Interfaces:**
- Consumes: `PaperStore.get_setting(key)`, `PaperStore.set_setting(key, value)`, Trader策略资格查询和现有渠道订单状态。
- Produces: `ChannelStrategyBinding`, `load_channel_binding(store)`, `save_channel_binding(store, binding)`, `bind_channel_strategy(store, release, actor, reason, active_orders)`；`PaperTradingEngine`显式接收绑定策略身份并传给`CliAdviceClient.get_decision(...)`。

- [ ] **Step 1: 写绑定迁移、幂等、活动订单阻塞和JSON往返的失败测试**

```python
def test_binding_round_trip_and_idempotence(store):
    release = {"strategy_id": "S001", "version": "v2", "release_id": "S001-v2", "release_hash": "abc"}
    first = bind_channel_strategy(store, release, "tomxiao", "人工确认", [])
    second = bind_channel_strategy(store, release, "tomxiao", "重复执行", [])
    assert load_channel_binding(store) == first
    assert second == first

def test_binding_rejects_switch_with_active_order(store):
    with pytest.raises(ChannelBindingError, match="活动渠道订单"):
        bind_channel_strategy(store, RELEASE_V2, "tomxiao", "切换", [{"order_id": "1", "status": "SUBMITTED"}])
```

- [ ] **Step 2: 运行定向测试确认失败**

Run: `python -m pytest packages/paper_trading_engine/tests/test_channel_binding.py -q`

Expected: FAIL，原因是`paper_trading_engine.channel_binding`尚不存在。

- [ ] **Step 3: 实现不可变绑定模型、持久化、历史决策迁移和审计事件**

```python
@dataclass(frozen=True)
class ChannelStrategyBinding:
    strategy_id: str
    strategy_version: str
    release_id: str
    release_hash: str
    bound_at: str
    bound_by: str
    reason: str

def load_channel_binding(store: PaperStore) -> ChannelStrategyBinding | None: ...
def bind_channel_strategy(store: PaperStore, release: dict[str, object], actor: str,
                          reason: str, active_orders: list[dict[str, object]]) -> ChannelStrategyBinding: ...
```

实现要求：字段缺失即报错；同一`release_id + release_hash`返回原绑定；切换前识别`SUBMITTED/PARTIAL_FILLED`等活动状态；成功切换写`futu_strategy_binding`设置和包含旧、新绑定、actor、reason的事件；历史迁移只读取最近渠道决策中的完整发布身份。

- [ ] **Step 4: 增加`pte channel bind-strategy`并让引擎使用绑定身份生成决策**

```powershell
pte channel bind-strategy --repo-root . --channel futu `
  --strategy S001 --strategy-version v2 --actor tomxiao --reason "人工确认"
```

CLI先调用Trader策略查询校验`qualification=qualified`，再写绑定；未绑定或决策身份与绑定不一致时，引擎继续对账并阻止新渠道订单。

- [ ] **Step 5: 运行绑定、CLI和引擎测试**

Run: `python -m pytest packages/paper_trading_engine/tests/test_channel_binding.py packages/paper_trading_engine/tests/test_cli.py packages/paper_trading_engine/tests/test_engine.py -q`

Expected: PASS。

- [ ] **Step 6: 提交绑定能力**

```powershell
git add packages/paper_trading_engine/src/paper_trading_engine/channel_binding.py packages/paper_trading_engine/src/paper_trading_engine/cli.py packages/paper_trading_engine/src/paper_trading_engine/engine.py packages/paper_trading_engine/tests/test_channel_binding.py packages/paper_trading_engine/tests/test_cli.py
git commit -m "feat: add explicit PTE channel strategy binding"
```

### Task 2: 账户级、渠道级和比较快照API

**Files:**
- Create: `packages/paper_trading_engine/src/paper_trading_engine/web_api.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/coordinator.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/store.py`
- Test: `packages/paper_trading_engine/tests/test_web_api.py`

**Interfaces:**
- Consumes: `PteCoordinator.status()`, `VirtualAccountEngine.status(account_id)`, `PaperStore.virtual_*`, `load_channel_binding(store)`。
- Produces: `PteWebApi.system_status()`, `virtual_accounts()`, `virtual_account_snapshot(account_id)`, `channel_snapshot("futu")`, `comparison(account_ids)`；每个资源快照包含`scope`和`as_of`。

- [ ] **Step 1: 写两个账户严格隔离和未知账户的失败测试**

```python
def test_account_snapshot_is_strictly_scoped(api):
    snap = api.virtual_account_snapshot("s001-v2")
    assert snap["scope"]["account_id"] == "s001-v2"
    assert {row["account_id"] for row in snap["orders"]} <= {"s001-v2"}
    assert {row["account_id"] for row in snap["fills"]} <= {"s001-v2"}

def test_unknown_account_raises_resource_not_found(api):
    with pytest.raises(ResourceNotFound):
        api.virtual_account_snapshot("missing")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest packages/paper_trading_engine/tests/test_web_api.py -q`

Expected: FAIL，原因是`PteWebApi`尚不存在。

- [ ] **Step 3: 实现资源快照门面和告警显式归属**

```python
class PteWebApi:
    def system_status(self) -> dict[str, object]: ...
    def virtual_accounts(self) -> dict[str, object]: ...
    def virtual_account_snapshot(self, account_id: str) -> dict[str, object]: ...
    def channel_snapshot(self, channel: str) -> dict[str, object]: ...
    def comparison(self, account_ids: list[str]) -> dict[str, object]: ...
```

账户快照只从指定`account_id`读取决策、模型订单、模型成交、绩效和账户事件；渠道快照只返回Futu账户、绑定、渠道决策、渠道订单、成交及渠道事件；系统状态只返回进程、调度、数据截止、发布和全局告警。

- [ ] **Step 4: 增加共同观察区间比较和不完整指标语义**

比较响应返回`common_window.start/end`及各账户累计收益、最大回撤、卡玛、盈亏比；无已闭合交易时`win_loss_ratio=null`并返回现有`win_loss_ratio_status`，禁止把不可用展示为0。

- [ ] **Step 5: 运行API与协调器测试**

Run: `python -m pytest packages/paper_trading_engine/tests/test_web_api.py packages/paper_trading_engine/tests/test_coordinator.py packages/paper_trading_engine/tests/test_store.py -q`

Expected: PASS。

- [ ] **Step 6: 提交资源API**

```powershell
git add packages/paper_trading_engine/src/paper_trading_engine/web_api.py packages/paper_trading_engine/src/paper_trading_engine/coordinator.py packages/paper_trading_engine/src/paper_trading_engine/store.py packages/paper_trading_engine/tests/test_web_api.py
git commit -m "feat: add scoped PTE console API"
```

### Task 3: HTTP资源路由与静态资源发布

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/web.py`
- Create: `packages/paper_trading_engine/src/paper_trading_engine/static/index.html`
- Create: `packages/paper_trading_engine/src/paper_trading_engine/static/app.js`
- Create: `packages/paper_trading_engine/src/paper_trading_engine/static/styles.css`
- Modify: `packages/paper_trading_engine/pyproject.toml`
- Test: `packages/paper_trading_engine/tests/test_web.py`

**Interfaces:**
- Consumes: Task 2的`PteWebApi`方法和现有pause/resume/cancel协调器命令。
- Produces: 设计文档第6节全部HTTP端点；`/accounts/*`、`/channels/futu`、`/comparison`均返回同一个应用壳；`/static/app.js`和`/static/styles.css`返回正确MIME类型。

- [ ] **Step 1: 写GET深链接、资源接口、未知账户404和兼容接口测试**

```python
def test_account_deep_link_serves_console(http_client):
    response = http_client.get("/accounts/s001-v2")
    assert response.status == 200
    assert b'<script src="/static/app.js"' in response.body

def test_unknown_account_api_returns_404(http_client):
    response = http_client.get("/api/virtual-accounts/missing/snapshot")
    assert response.status == 404
```

- [ ] **Step 2: 运行Web测试确认失败**

Run: `python -m pytest packages/paper_trading_engine/tests/test_web.py -q`

Expected: FAIL，缺少新路由或静态资源。

- [ ] **Step 3: 将`web.py`收敛为路由、MIME和异常映射**

GET实现`/api/system/status`、`/api/virtual-accounts`、账户快照、Futu快照、比较接口和兼容`/api/status`；POST实现账户暂停/恢复、渠道暂停/恢复、撤单令牌和撤单；响应中的资源身份与URL一致，`ResourceNotFound`映射404，输入错误映射400，冲突映射409。

- [ ] **Step 4: 建立静态应用壳并声明包数据**

```toml
[tool.setuptools.package-data]
paper_trading_engine = ["static/*.html", "static/*.js", "static/*.css"]
```

`index.html`只包含语义容器、导航和静态资源引用，禁止内嵌旧`dashboard.py`的聚合页面脚本。

- [ ] **Step 5: 运行Web测试和构建wheel检查静态文件**

Run: `python -m pytest packages/paper_trading_engine/tests/test_web.py -q`

Run: `python -m build packages/paper_trading_engine --wheel`

Expected: 测试PASS；wheel中包含`static/index.html`、`static/app.js`、`static/styles.css`。

- [ ] **Step 6: 提交HTTP和静态资源骨架**

```powershell
git add packages/paper_trading_engine/src/paper_trading_engine/web.py packages/paper_trading_engine/src/paper_trading_engine/static packages/paper_trading_engine/pyproject.toml packages/paper_trading_engine/tests/test_web.py
git commit -m "feat: serve resource-scoped PTE console"
```

### Task 4: URL驱动的虚拟账户主从工作区

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/static/app.js`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/static/styles.css`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/static/index.html`
- Create: `packages/paper_trading_engine/tests/js/console_state.test.mjs`

**Interfaces:**
- Consumes: `/api/system/status`、`/api/virtual-accounts`、`/api/virtual-accounts/{account_id}/snapshot`。
- Produces: `parseRoute(pathname)`, `ScopedLoader`, `renderAccountList(accounts)`, `renderAccountSnapshot(snapshot)`；账户切换用`history.pushState`更新URL。

- [ ] **Step 1: 写路由解析和迟到响应丢弃的Node失败测试**

```javascript
assert.deepEqual(parseRoute('/accounts/s001-v2'), {page: 'account', accountId: 's001-v2'});
const loader = new ScopedLoader();
const oldRequest = loader.begin('baseline-143');
const newRequest = loader.begin('s001-v2');
assert.equal(loader.accept(oldRequest, 'baseline-143'), false);
assert.equal(loader.accept(newRequest, 's001-v2'), true);
```

- [ ] **Step 2: 运行Node测试确认失败**

Run: `node --test packages/paper_trading_engine/tests/js/console_state.test.mjs`

Expected: FAIL，缺少导出的路由与请求生命周期函数。

- [ ] **Step 3: 实现URL唯一事实源、AbortController和响应scope校验**

进入`/`时才读取`localStorage.pteLastAccountId`决定重定向；进入账户深链接完全服从URL。每次切换先清空旧业务值、递增序号并中止旧请求；渲染前同时校验请求序号、`scope.account_id`、`release_id`。

- [ ] **Step 4: 实现账户列表与完整账户工作区**

工作区依序显示账户身份、策略`release_id`/哈希/资格、账户开关、资金持仓、最新决策与未下单原因、模型订单、模型成交、最大回撤/卡玛/盈亏比和审计；全部订单行明确标注`account_id`，且没有撤单按钮。

- [ ] **Step 5: 实现桌面、窄屏与加载/过期/错误视觉状态**

桌面为左侧固定宽度账户列表和右侧工作区；窄屏将列表改为顶部横向卡片，表格容器内部滚动。失败时保留账户标题，业务值统一显示“不可用”，并显示错误和最后成功时间。

- [ ] **Step 6: 运行JS测试和语法检查**

Run: `node --test packages/paper_trading_engine/tests/js/console_state.test.mjs`

Run: `node --check packages/paper_trading_engine/src/paper_trading_engine/static/app.js`

Expected: PASS。

- [ ] **Step 7: 提交虚拟账户工作区**

```powershell
git add packages/paper_trading_engine/src/paper_trading_engine/static packages/paper_trading_engine/tests/js/console_state.test.mjs
git commit -m "feat: add account-scoped PTE workspace"
```

### Task 5: Futu渠道页与账户比较页

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/static/app.js`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/static/styles.css`
- Test: `packages/paper_trading_engine/tests/js/console_state.test.mjs`
- Test: `packages/paper_trading_engine/tests/test_web.py`

**Interfaces:**
- Consumes: `/api/channels/futu/snapshot`、渠道pause/resume/cancel端点和`/api/comparison`。
- Produces: `renderChannelSnapshot(snapshot)`、`renderComparison(snapshot)`；撤单二次确认只存在于Futu页。

- [ ] **Step 1: 写页面权限边界和比较参数的失败测试**

```javascript
assert.equal(actionsForRoute({page: 'account'}).includes('cancel'), false);
assert.equal(actionsForRoute({page: 'comparison'}).length, 0);
assert.equal(actionsForRoute({page: 'channel'}).includes('cancel'), true);
assert.equal(comparisonQuery(['a', 'b']), 'account_id=a&account_id=b');
```

- [ ] **Step 2: 运行定向测试确认失败**

Run: `node --test packages/paper_trading_engine/tests/js/console_state.test.mjs`

Expected: FAIL，缺少页面动作边界和比较查询函数。

- [ ] **Step 3: 实现Futu渠道专属页**

展示连接、行情、资金、实际持仓、绑定`release_id`/哈希/时间/原因、渠道决策、渠道订单、成交、退避和渠道事件。暂停开关只阻止新渠道订单；撤单依次请求两分钟令牌并在确认框明确显示渠道、订单号、方向、数量和策略版本。

- [ ] **Step 4: 实现只读账户比较页**

默认选中全部虚拟账户，允许多选；用共同观察区间展示累计收益、最大回撤、卡玛和盈亏比。比较页DOM不生成暂停、恢复、绑定或撤单按钮。

- [ ] **Step 5: 运行JS和HTTP边界测试**

Run: `node --test packages/paper_trading_engine/tests/js/console_state.test.mjs`

Run: `python -m pytest packages/paper_trading_engine/tests/test_web.py -q`

Expected: PASS。

- [ ] **Step 6: 提交渠道和比较页面**

```powershell
git add packages/paper_trading_engine/src/paper_trading_engine/static packages/paper_trading_engine/tests/js/console_state.test.mjs packages/paper_trading_engine/tests/test_web.py
git commit -m "feat: add PTE channel and comparison views"
```

### Task 6: 迁移、文档、全量回归与8080验收

**Files:**
- Modify: `packages/paper_trading_engine/README.md`
- Modify: `docs/DEV_HANDOFF.md`
- Modify: `docs/superpowers/specs/2026-09-03-pte-console-v2-design.md`
- Test: `packages/paper_trading_engine/tests/test_watchdog.py`

**Interfaces:**
- Consumes: Tasks 1-5的完整实现和现有Windows服务`CZSC-PTE-Watchdog`。
- Produces: 可跨机继续维护的使用说明、已批准/已实现设计状态、全量测试证据和真实8080验收记录。

- [ ] **Step 1: 更新安装、URL、策略绑定和干预说明**

README写明三个入口、虚拟账户与Futu渠道边界、绑定CLI、暂停语义、撤单二次确认及`/api/status`兼容用途；开发交接文档记录分支、架构、数据迁移和Windows服务重启方法。

- [ ] **Step 2: 将设计状态更新为“已批准并实现”**

同时记录冻结新策略只创建虚拟账户、Futu绑定必须显式执行，以及Console v2不消费聚合`/api/status`。

- [ ] **Step 3: 运行PTE全量测试、JS测试、ruff和静态资源检查**

Run: `python -m pytest packages/paper_trading_engine/tests -q`

Run: `node --test packages/paper_trading_engine/tests/js/console_state.test.mjs`

Run: `python -m ruff check packages/paper_trading_engine/src packages/paper_trading_engine/tests`

Run: `node --check packages/paper_trading_engine/src/paper_trading_engine/static/app.js`

Expected: 全部PASS。

- [ ] **Step 4: 提交文档与收尾改动**

```powershell
git add packages/paper_trading_engine/README.md docs/DEV_HANDOFF.md docs/superpowers/specs/2026-09-03-pte-console-v2-design.md packages/paper_trading_engine/tests/test_watchdog.py
git commit -m "docs: document PTE console v2 operations"
```

- [ ] **Step 5: 重启真实服务并执行人工验收**

管理员PowerShell运行：

```powershell
Restart-Service -Name 'CZSC-PTE-Watchdog' -Force
```

验收`http://127.0.0.1:8080/accounts/baseline-143`、`/accounts/s001-v2`、`/channels/futu`和`/comparison`；核对切换、刷新、身份、订单、成交、开关和撤单边界，并用窄屏检查无横向覆盖。

- [ ] **Step 6: 记录最终分支状态并等待合并/推送授权**

Run: `git status --short --branch`

Expected: 工作树干净，分支为`codex/pte-console-v2`；向用户报告提交、测试和验收结果，在获得明确授权后再合并master和推送远端。

## Self-Review

- Spec coverage: Tasks 1-6覆盖领域不变量、三页信息架构、显式绑定、资源级HTTP契约、URL与轮询、告警归属、代码拆分、操作安全、自动测试、真实8080验收和交付文档。
- Placeholder scan: 计划中没有`TBD`、`TODO`或未指定的“后续实现”；每项测试、接口、命令和验收结果均已明确。
- Type consistency: `ChannelStrategyBinding`由Task 1产生并被Task 2渠道快照消费；`PteWebApi`由Task 2产生并被Task 3路由消费；Task 4-5只消费Task 3发布的资源级端点。
