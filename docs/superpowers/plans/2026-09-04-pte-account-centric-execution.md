# PTE Account-Centric Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace PTE's dual Futu/virtual execution engines with one account-centric engine that routes every logical account through the shared Futu simulation channel.

**Architecture:** `AccountEngine` generates decisions and durable order intents per logical account. `FutuExecution` submits those intents, attributes broker orders and fill increments, updates the owning account ledger, and reconciles broker aggregates. `PteCoordinator` exposes one runtime surface; the OHLC fill engine and channel-to-strategy binding are removed.

**Tech Stack:** Python 3.12, SQLite, standard-library HTTP and JavaScript, pytest, Futu OpenAPI adapter

**Spec:** `docs/superpowers/specs/2026-09-04-pte-account-centric-execution-design.md`

## Global Constraints

- The only execution channel is `futu`, hard-locked to `TrdEnv.SIMULATE` and China market.
- Every account has immutable strategy identity, immutable `channel_id=futu`, and default CNY 100,000 allocation.
- Every decision, intent, order, and fill belongs to exactly one `account_id`.
- Futu fills are the only execution facts; OHLC fills and automatic fallback are forbidden.
- Accounts never merge orders, net positions, or borrow another account's allocation.
- Existing audit events and legacy channel snapshots remain unchanged.
- Migration aborts when any legacy intent/order/fill table contains trading records.
- PTE continues to call Trader only through CLI.

---

### Task 1: Account-Centric Store and Migration

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/store.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/audit.py`
- Test: `packages/paper_trading_engine/tests/functional/test_virtual_accounts.py`

**Interfaces:**
- Produces: `save_account_decision`, `create_account_intent`, `bind_channel_order`, `apply_fill_increment`, `account_orders`, `account_fills`, `save_account_snapshot`.
- Produces: `virtual_accounts.channel_id`, `virtual_accounts.status`, and guarded schema migration.

- [ ] **Step 1: Write a failing independent-ledger test**

```python
def test_accounts_share_futu_and_keep_independent_ledgers(tmp_path):
    store = PaperStore(tmp_path / "runtime.db")
    create_account(store, "s001-v1", "S001", "v1", "a" * 64)
    create_account(store, "s001-v2", "S001", "v2", "b" * 64)
    assert store.virtual_account("s001-v1")["channel_id"] == "futu"
    intent = store.create_account_intent(
        "s001-v1", "DEC-1", 1, "588080.SH", "BUY", 1000, "1.680", "2026-09-04"
    )
    store.bind_channel_order(intent["intent_id"], "1001", "SUBMITTED")
    store.apply_fill_increment("1001", 1000, "1.670", "0.8350", "2026-09-04T01:31:00+00:00")
    assert store.virtual_account("s001-v1")["quantity"] == 1000
    assert store.virtual_account("s001-v2")["quantity"] == 0
```

- [ ] **Step 2: Run it and verify failure due to missing account-centric schema/API**

Run: `.venv\Scripts\python.exe -m pytest packages/paper_trading_engine/tests/functional/test_virtual_accounts.py -q`

- [ ] **Step 3: Implement the spec's account, decision, intent, order, fill, ledger, and snapshot schema**

Add transactionally consistent balance/position methods and capacity validation. Add new account binding and reconciliation event types while retaining historical `CHANNEL_STRATEGY_BOUND` as readable legacy evidence. Refuse migration when old trade tables contain rows; after the guard, remove the obsolete setting and empty model tables.

- [ ] **Step 4: Run the focused test and verify pass**

Run: `.venv\Scripts\python.exe -m pytest packages/paper_trading_engine/tests/functional/test_virtual_accounts.py -q`

- [ ] **Step 5: Commit**

Run: `git add packages/paper_trading_engine/src/paper_trading_engine/store.py packages/paper_trading_engine/src/paper_trading_engine/audit.py packages/paper_trading_engine/tests/functional/test_virtual_accounts.py; git commit -m "refactor: center PTE ledger on logical accounts"`

### Task 2: Account Decision Engine

**Files:**
- Create: `packages/paper_trading_engine/src/paper_trading_engine/account_engine.py`
- Modify: `packages/paper_trading_engine/tests/functional/test_trading_cycle.py`
- Modify: `packages/paper_trading_engine/tests/functional/pte_support.py`

**Interfaces:**
- Consumes: store API from Task 1 and `AdviceClient.get_decision`.
- Produces: `AccountEngine.refresh_account(account_id, force=False)` and `refresh_all()`.

- [ ] **Step 1: Write a failing decision ownership and idempotence test**

```python
def test_each_account_generates_one_owned_decision_and_intent(tmp_path):
    store, advice = account_runtime(tmp_path)
    engine = AccountEngine(store, advice)
    engine.refresh_account("s001-v1", force=True)
    engine.refresh_account("s001-v2", force=True)
    engine.refresh_all()
    assert {row["account_id"] for row in store.account_decisions()} == {"s001-v1", "s001-v2"}
    assert len(store.pending_account_intents()) == 2
    assert store.query_audit_events(event_type="DECISION_GENERATED", channel="futu") == []
```

- [ ] **Step 2: Run and verify missing `AccountEngine` failure**

Run: `.venv\Scripts\python.exe -m pytest packages/paper_trading_engine/tests/functional/test_trading_cycle.py -q`

- [ ] **Step 3: Implement account-scoped advice, identity validation, decisions, signals, pause behavior, and intent creation**

Use only the account's cash, quantity, cycle target and immutable release. Save decisions before intents. Generate stable per-account intent identities and freeze only that account's resources. Decision/signal audit events carry account and strategy without channel; intent events also carry `channel=futu`.

- [ ] **Step 4: Run and verify pass**

Run: `.venv\Scripts\python.exe -m pytest packages/paper_trading_engine/tests/functional/test_trading_cycle.py -q`

- [ ] **Step 5: Commit**

Run: `git add packages/paper_trading_engine/src/paper_trading_engine/account_engine.py packages/paper_trading_engine/tests/functional/test_trading_cycle.py packages/paper_trading_engine/tests/functional/pte_support.py; git commit -m "refactor: generate decisions by PTE account"`

### Task 3: Shared Futu Execution and Reconciliation

**Files:**
- Create: `packages/paper_trading_engine/src/paper_trading_engine/futu_execution.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/futu_gateway.py`
- Modify: `packages/paper_trading_engine/tests/functional/test_channel_safety.py`
- Modify: `packages/paper_trading_engine/tests/functional/test_trading_cycle.py`

**Interfaces:**
- Consumes: pending account intents and ledger API.
- Produces: `FutuExecution.reconcile`, `submit_pending`, `refresh`, `pause`, `resume`, and account-scoped cancellation.

- [ ] **Step 1: Write failing routing and unknown-activity tests**

```python
def test_futu_routes_fill_to_owning_account(runtime):
    runtime.execution.submit_pending()
    runtime.broker.fill(account_id="s001-v2", quantity=500, price=1.67)
    runtime.execution.reconcile()
    assert runtime.store.virtual_account("s001-v1")["quantity"] == 0
    assert runtime.store.virtual_account("s001-v2")["quantity"] == 500

def test_unknown_futu_order_blocks_new_submissions(runtime):
    runtime.broker.add_external_order("UNOWNED")
    with pytest.raises(ChannelReconciliationError):
        runtime.execution.submit_pending()
```

- [ ] **Step 2: Run and verify missing shared execution failure**

Run: `.venv\Scripts\python.exe -m pytest packages/paper_trading_engine/tests/functional/test_channel_safety.py packages/paper_trading_engine/tests/functional/test_trading_cycle.py -q`

- [ ] **Step 3: Implement remark mapping, pre-submit reconciliation, ordered routing, fill increments, restart recovery, and scoped cancellation**

Every remark resolves to one durable intent. Reconcile before submissions, reject unknown activity and aggregate mismatches, submit without netting, and apply cumulative fill deltas once. Cancellation requires matching `account_id` and channel order ID.

- [ ] **Step 4: Run and verify pass**

Run: `.venv\Scripts\python.exe -m pytest packages/paper_trading_engine/tests/functional/test_channel_safety.py packages/paper_trading_engine/tests/functional/test_trading_cycle.py -q`

- [ ] **Step 5: Commit**

Run: `git add packages/paper_trading_engine/src/paper_trading_engine/futu_execution.py packages/paper_trading_engine/src/paper_trading_engine/futu_gateway.py packages/paper_trading_engine/tests/functional/test_channel_safety.py packages/paper_trading_engine/tests/functional/test_trading_cycle.py; git commit -m "refactor: route multiple accounts through Futu"`

### Task 4: Runtime, Scheduler, and CLI Cutover

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/coordinator.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/scheduler.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/cli.py`
- Delete: `packages/paper_trading_engine/src/paper_trading_engine/channel_binding.py`
- Delete: `packages/paper_trading_engine/src/paper_trading_engine/virtual_engine.py`
- Delete: `packages/paper_trading_engine/src/paper_trading_engine/virtual_fill.py`
- Replace/Delete: `packages/paper_trading_engine/src/paper_trading_engine/engine.py`
- Test: `packages/paper_trading_engine/tests/functional/test_scheduler.py`

**Interfaces:**
- Consumes: `AccountEngine` and `FutuExecution`.
- Produces: one `PteCoordinator` for CLI, Scheduler, watchdog, and Web API.

- [ ] **Step 1: Write a failing publish-then-all-accounts scheduler test**

```python
def test_publish_generates_all_account_decisions_without_model_fill(runtime_probe):
    runtime_probe.scheduler.tick(datetime(2026, 9, 3, 19, 0))
    assert runtime_probe.calls == [
        "publish:2026-09-03", "decide:s001-v1", "decide:s001-v2"
    ]
```

- [ ] **Step 2: Run and verify the dual-engine sequence fails**

Run: `.venv\Scripts\python.exe -m pytest packages/paper_trading_engine/tests/functional/test_scheduler.py -q`

- [ ] **Step 3: Build one coordinator and remove model settlement, strategy binding, reference-account, and channel decision code**

Scheduler publishes once, refreshes every account, reconciles/submits in the valid window, and polls existing orders independently. Remove `channel bind-strategy`, `is_futu_reference`, all deleted-module imports, and channel-side advice generation.

- [ ] **Step 4: Run scheduler, watchdog, and channel tests**

Run: `.venv\Scripts\python.exe -m pytest packages/paper_trading_engine/tests/functional/test_scheduler.py packages/paper_trading_engine/tests/functional/test_watchdog_service.py packages/paper_trading_engine/tests/functional/test_channel_safety.py -q`

- [ ] **Step 5: Commit**

Run: `git add -A packages/paper_trading_engine/src/paper_trading_engine packages/paper_trading_engine/tests/functional; git commit -m "refactor: run one account-centric PTE engine"`

### Task 5: API, Console, and Audit Semantics

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/web_api.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/web.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/static/index.html`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/static/app.js`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/static/styles.css`
- Modify: `packages/paper_trading_engine/tests/functional/test_web_console.py`
- Modify: `packages/paper_trading_engine/tests/functional/console_state.test.mjs`

**Interfaces:**
- Consumes: account-centric coordinator/store APIs.
- Produces: scoped account snapshots, aggregate Futu snapshot with account allocations, and account-scoped cancel endpoints.

- [ ] **Step 1: Write failing API/DOM assertions**

```python
def test_futu_snapshot_lists_accounts_without_binding_or_channel_decision(api):
    snapshot = api.channel_snapshot("futu")
    assert [row["account_id"] for row in snapshot["accounts"]] == ["s001-v1", "s001-v2"]
    assert "binding" not in snapshot
    assert "decision" not in snapshot
    assert all(order["account_id"] for order in snapshot["orders"])
```

The JavaScript test asserts sections `资金分配`, `承载账户`, `订单`, and `成交`, with no `当前执行策略` or `最新渠道决策`.

- [ ] **Step 2: Run and verify the old Futu binding UI fails**

Run: `.venv\Scripts\python.exe -m pytest packages/paper_trading_engine/tests/functional/test_web_console.py -q; node packages/paper_trading_engine/tests/functional/console_state.test.mjs`

- [ ] **Step 3: Implement account-first API payloads, cancellation scope, and console rendering**

Account pages own decisions. Futu page shows total account, unallocated funds, bound account list, account-labeled orders/fills, reconciliation and channel events. Render old `CHANNEL_STRATEGY_BOUND` as `历史关系 · 渠道曾直接绑定策略` and remove active strategy-binding controls/text.

- [ ] **Step 4: Run and verify pass**

Run: `.venv\Scripts\python.exe -m pytest packages/paper_trading_engine/tests/functional/test_web_console.py -q; node packages/paper_trading_engine/tests/functional/console_state.test.mjs`

- [ ] **Step 5: Commit**

Run: `git add packages/paper_trading_engine/src/paper_trading_engine/web_api.py packages/paper_trading_engine/src/paper_trading_engine/web.py packages/paper_trading_engine/src/paper_trading_engine/static packages/paper_trading_engine/tests/functional/test_web_console.py packages/paper_trading_engine/tests/functional/console_state.test.mjs; git commit -m "feat: show account-owned Futu execution"`

### Task 6: Documentation and Live Delivery

**Files:**
- Modify: `packages/paper_trading_engine/README.md`
- Modify: `README.md`
- Modify: `docs/DEVELOPMENT_HANDOFF.md`
- Modify: `docs/superpowers/specs/2026-09-04-pte-audit-events-design.md`

**Interfaces:**
- Produces: operator guidance and a verified migration of `state/paper_trading/runtime.db`.

- [ ] **Step 1: Document the final account/channel model and remove obsolete commands**

Cover account creation, 10万元 allocation, Futu-exclusive ownership, timing, pause/resume, audit ownership, restart recovery, and the absence of model execution.

- [ ] **Step 2: Run complete retained PTE tests and static checks**

Run: `.venv\Scripts\python.exe -m pytest packages/paper_trading_engine/tests/functional -q`

Run: `.venv\Scripts\python.exe -m compileall -q packages/paper_trading_engine/src`

Run: `node packages/paper_trading_engine/tests/functional/console_state.test.mjs`

Run: `git diff --check`

- [ ] **Step 3: Peacefully stop PTE, back up the runtime DB, and start the guarded migration**

Use the existing restart endpoint. Copy the DB to a timestamped file under the untracked `state/paper_trading` directory, retain the backup, and let the new `PaperStore` migrate on startup.

- [ ] **Step 4: Verify live invariants**

Verify port 8080, both accounts with `channel_id=futu`, absence of obsolete binding settings/model tables, Futu snapshot account list, absence of channel decision/binding, Beijing audit timestamps, and watchdog health.

- [ ] **Step 5: Commit documentation and verification fixes**

Run: `git add README.md packages/paper_trading_engine/README.md docs/DEVELOPMENT_HANDOFF.md docs/superpowers/specs/2026-09-04-pte-audit-events-design.md; git commit -m "docs: hand off account-centric PTE runtime"`

- [ ] **Step 6: Report branch result without merging or pushing**

Run: `git status --short --branch; git log -8 --oneline`

