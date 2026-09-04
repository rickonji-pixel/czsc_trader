# PTE Audit Events Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade PTE's existing SQLite events table into one typed, queryable audit ledger covering strategy, trading, system, and fallback events.

**Architecture:** Add immutable audit models and a central recorder, migrate the existing table in place, then instrument the current strategy, execution, adapter, scheduler, and lifecycle boundaries. Expose one filtered cursor API and one console page while retaining `PaperStore.add_event` as a compatibility facade.

**Tech Stack:** Python 3.12, dataclasses, enums, SQLite, standard-library HTTP server, vanilla JavaScript, pytest, Node test runner.

**Spec:** `docs/superpowers/specs/2026-09-04-pte-audit-events-design.md`

## Global Constraints

- Keep one append-only SQLite event ledger and preserve all legacy payloads.
- Persist UTC ISO 8601 timestamps and render Beijing time in the console.
- Use exactly `STRATEGY`, `TRADING`, `SYSTEM`, and `OTHER` categories.
- Do not persist tokens, passwords, secrets, or complete credentials.
- Record every mutating external call; record read-only polling only on failure, recovery, or health transition.
- Preserve the consolidated seven Python PTE scenarios and one JavaScript scenario.
- Do not change strategy logic, order pricing, broker environment, or account balances.

---

### Task 1: Audit Contract and Catalog

**Files:**
- Create: `packages/paper_trading_engine/src/paper_trading_engine/audit.py`
- Modify: `packages/paper_trading_engine/tests/functional/test_performance_evidence.py`

**Interfaces:**
- Produces: `AuditCategory`, `AuditSeverity`, `AuditOutcome`, `AuditEvent`, `AuditRecorder`.
- Produces: `AuditRecorder.record(event_type: str, *, source: str, outcome: AuditOutcome | str = "SUCCESS", severity: AuditSeverity | str | None = None, correlation_id: str | None = None, actor_type: str = "ENGINE", actor_id: str | None = None, account_id: str | None = None, strategy_id: str | None = None, strategy_version: str | None = None, release_hash: str | None = None, symbol: str | None = None, channel: str | None = None, decision_id: str | None = None, order_id: str | None = None, details: dict[str, object] | None = None) -> dict[str, object]`.
- Consumes: a store exposing `append_audit_event(AuditEvent)`.

- [ ] **Step 1: Extend FT-PTE07 with failing contract assertions**

Add assertions that a catalog event resolves its category/default severity, an unknown event is rejected, `OTHER` requires `classification_reason`, models are immutable, and nested keys matching `token`, `password`, `secret`, or `credential` are replaced with `"[REDACTED]"`.

```python
event = recorder.record("DECISION_GENERATED", source="engine", decision_id="DEC-1")
assert event["category"] == "STRATEGY"
with pytest.raises(AuditContractError):
    recorder.record("UNKNOWN", source="engine")
```

- [ ] **Step 2: Run FT-PTE07 and confirm the audit imports fail**

Run: `.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests\functional\test_performance_evidence.py -q`

- [ ] **Step 3: Implement immutable models, complete catalog, validation, UUID generation, and recursive redaction**

Use `@dataclass(frozen=True)` and string enums. The catalog contains every type in design section 5. `AuditEvent.to_dict()` returns JSON-compatible values and exposes stored `payload` as `details`.

- [ ] **Step 4: Run FT-PTE07 and Ruff**

Run the test above and `.\.venv\Scripts\python.exe -m ruff check packages\paper_trading_engine\src\paper_trading_engine\audit.py`.

- [ ] **Step 5: Commit the contract**

```powershell
git add packages/paper_trading_engine/src/paper_trading_engine/audit.py packages/paper_trading_engine/tests/functional/test_performance_evidence.py
git commit -m "feat: define PTE audit event contract"
```

### Task 2: In-Place Ledger Migration and Queries

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/store.py`
- Modify: `packages/paper_trading_engine/tests/functional/test_virtual_accounts.py`

**Interfaces:**
- Produces: `PaperStore.append_audit_event(event: AuditEvent) -> dict[str, object]`.
- Produces: `PaperStore.query_audit_events(*, category=None, event_type=None, severity=None, outcome=None, account_id=None, strategy_id=None, channel=None, decision_id=None, order_id=None, correlation_id=None, before_id=None, limit=50) -> list[dict[str, object]]`.
- Preserves: `add_event(event_type, payload)` and `recent_events(limit)`.

- [ ] **Step 1: Extend FT-PTE01 with a legacy database migration fixture**

Create the old three-column events table directly, insert known and unknown events, reopen through `PaperStore`, and assert count/payload preservation, deterministic UUIDv5, category mappings, scope extraction, indexes, and idempotent reopen.

- [ ] **Step 2: Run FT-PTE01 and confirm new columns/query API are absent**

Run: `.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests\functional\test_virtual_accounts.py -q`

- [ ] **Step 3: Add nullable columns, backfill transaction, version marker, and indexes**

Use `_ensure_column` followed by one transaction. Derive legacy UUIDv5 from `id|created_at|event_type`; preserve `payload`; map known types from design section 10; store unknown original type in `details.legacy_event_type` and a migration classification reason.

- [ ] **Step 4: Implement append, compatibility, filtered cursor queries, and immutable public API**

Reject limits outside `1..200`. `recent_events` delegates to `query_audit_events` and continues returning `created_at`, `event_type`, and `payload` aliases alongside the new contract.

- [ ] **Step 5: Run FT-PTE01, FT-PTE07, and Ruff**

Run both functional files and Ruff on `store.py`, `audit.py`, and their tests.

- [ ] **Step 6: Commit the ledger migration**

```powershell
git add packages/paper_trading_engine/src/paper_trading_engine/store.py packages/paper_trading_engine/tests/functional
git commit -m "feat: migrate PTE audit event ledger"
```

### Task 3: Strategy and Scheduler Event Production

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/data_publisher.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/advice_client.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/scheduler.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/engine.py`
- Modify: `packages/paper_trading_engine/tests/functional/test_scheduler.py`
- Modify: `packages/paper_trading_engine/tests/functional/test_trading_cycle.py`

**Interfaces:**
- Each adapter accepts optional `audit: AuditRecorder | None`.
- Publication correlation is `publication:{YYYY-MM-DD}`.
- Valid decisions use `decision_id` as correlation ID.

- [ ] **Step 1: Add failing FT-PTE04/02 assertions for the complete publication-to-signal chain**

Assert requested/published/failed events, Trader external call duration/outcome, one `DECISION_GENERATED`, one `SIGNAL_TRIGGERED`, expiration/block events only on state change, shared correlation IDs, and no duplicate event after repeated five-second refresh.

- [ ] **Step 2: Run both scenarios and confirm missing event types**

Run FT-PTE02 and FT-PTE04 by node id.

- [ ] **Step 3: Inject the recorder and instrument publication/advice calls**

Use `time.perf_counter()` for `duration_ms`. Record mutating/critical calls on both success and failure; sanitize stderr and exception text through the recorder.

- [ ] **Step 4: Instrument validated decisions, signal changes, expiration, and scheduler transitions**

Persist decision/signal deduplication keys in SQLite settings so process restart does not duplicate the same fact. Keep existing scheduler backoff behavior unchanged.

- [ ] **Step 5: Run FT-PTE02/04 and Ruff, then commit**

```powershell
git add packages/paper_trading_engine/src/paper_trading_engine packages/paper_trading_engine/tests/functional
git commit -m "feat: audit PTE strategy lifecycle"
```

### Task 4: Trading, Futu, and Virtual Account Events

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/engine.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/futu_gateway.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/virtual_engine.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/store.py`
- Modify: `packages/paper_trading_engine/tests/functional/test_channel_safety.py`
- Modify: `packages/paper_trading_engine/tests/functional/test_trading_cycle.py`
- Modify: `packages/paper_trading_engine/tests/functional/test_virtual_accounts.py`

**Interfaces:**
- Futu Gateway records `EXTERNAL_CALL_SUCCEEDED/FAILED` for place/cancel.
- Engine records typed intent, submit, block, cancel, partial/full fill, and termination events.
- Virtual orders use the same event types with `channel="virtual"` and mandatory `account_id`.

- [ ] **Step 1: Add failing assertions covering buy, sell, reject, cancel, partial/full fill, recovery, virtual isolation, and read-only polling silence**

Also simulate recorder persistence failure before Futu submission and assert `place_order` is not called.

- [ ] **Step 2: Run FT-PTE01/02/03 and confirm missing typed events**

- [ ] **Step 3: Make state transition and event writes atomic where both are local**

Add store methods that write intent/event and order binding/event in one connection transaction. Preserve remark-based recovery for crashes between external submission and local binding.

- [ ] **Step 4: Instrument Futu mutations and health transitions without successful polling noise**

Record service `futu`, operations `place_order` and `cancel_order`, duration, outcome, and redacted error. Emit dependency degraded/recovered only when the state changes.

- [ ] **Step 5: Instrument virtual account decisions/orders/fills with immutable account and strategy scopes**

- [ ] **Step 6: Run FT-PTE01/02/03 and Ruff, then commit**

```powershell
git add packages/paper_trading_engine/src/paper_trading_engine packages/paper_trading_engine/tests/functional
git commit -m "feat: audit PTE trading lifecycle"
```

### Task 5: Lifecycle, HTTP Query, and Console

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/cli.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/coordinator.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/web_api.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/web.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/static/index.html`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/static/app.js`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/static/styles.css`
- Modify: `packages/paper_trading_engine/tests/functional/test_web_console.py`
- Modify: `packages/paper_trading_engine/tests/functional/console_state.test.mjs`

**Interfaces:**
- Produces: `PteWebApi.audit_events(filters: dict[str, str]) -> dict[str, object]`.
- Produces: `GET /api/audit-events` with `events` and `next_before_id`.
- Produces: `/audit-events` browser route.

- [ ] **Step 1: Add failing HTTP assertions for category/scope/correlation filters, cursor limits, unknown values, and read-only behavior**

- [ ] **Step 2: Add failing JS assertions for route parsing, Chinese labels, Beijing timestamps, formatted details, filters, and correlation navigation**

- [ ] **Step 3: Wire one shared recorder into runtime components and record service/control lifecycle events**

The build path creates one recorder from the runtime store and passes it to adapters/engines. Shutdown records `SERVICE_STOPPED` before closing SQLite.

- [ ] **Step 4: Implement validated API filtering and cursor response**

Return HTTP 400 for invalid categories/outcomes/limits and 404 only for unknown resource paths.

- [ ] **Step 5: Implement the audit page and reuse event rendering in account/Futu pages**

Use four category summary cards, filters, a compact event list, accessible expandable details, and material-state fingerprinting to avoid page flashes.

- [ ] **Step 6: Run FT-PTE05 and the Node scenario, inspect HTML/JS/CSS, then commit**

```powershell
git add packages/paper_trading_engine/src/paper_trading_engine packages/paper_trading_engine/tests/functional
git commit -m "feat: expose PTE audit event console"
```

### Task 6: Documentation and Full Verification

**Files:**
- Modify: `README.md`
- Modify: `packages/paper_trading_engine/README.md`
- Modify: `docs/DEVELOPMENT_HANDOFF.md`
- Modify: `docs/superpowers/plans/2026-09-04-pte-audit-events.md`

**Interfaces:** None.

- [ ] **Step 1: Document event semantics, API filters, UI entry, UTC storage/Beijing rendering, retention, and failure behavior**

- [ ] **Step 2: Run exact PTE collection and all seven Python scenarios**

Run: `.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests --collect-only -q`

Expected: exactly seven tests.

- [ ] **Step 3: Run PTE JavaScript and all four Python package suites**

```powershell
node --test packages\paper_trading_engine\tests\functional\console_state.test.mjs
.\.venv\Scripts\python.exe -m pytest tests\functional packages\strategy_manager\tests\functional packages\strategy_evaluator\tests\functional packages\paper_trading_engine\tests\functional -q
```

Expected: one JavaScript test and twenty Python tests pass.

- [ ] **Step 4: Run archive, Ruff, compile, dependency, and diff checks**

```powershell
.\.venv\Scripts\czsc-trader.exe archive validate --all --repo-root .
.\.venv\Scripts\python.exe -m ruff check src tests packages\strategy_manager packages\strategy_evaluator packages\paper_trading_engine
.\.venv\Scripts\python.exe -m compileall -q src packages\strategy_manager\src packages\strategy_evaluator\src packages\paper_trading_engine\src
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

Expected: 56 archives validate; all other commands exit zero.

- [ ] **Step 5: Confirm scope and commit delivery**

Confirm no files under `configs/`, `experiments/`, `data/`, `outputs/`, or `state/` changed, then commit:

```powershell
git add README.md packages/paper_trading_engine/README.md docs/DEVELOPMENT_HANDOFF.md docs/superpowers/plans/2026-09-04-pte-audit-events.md
git commit -m "docs: hand off PTE audit events"
```
