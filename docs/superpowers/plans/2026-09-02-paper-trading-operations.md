# Paper Trading Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and start a localhost page that observes and safely intervenes in the `SH.588080` paper-trading loop, with Futu as the first channel adapter.

**Architecture:** A broker-neutral coordinator persists snapshots and audit events to SQLite. A Futu gateway hard-locks paper trading, while a standard-library HTTP server exposes a small local dashboard and confirmed interventions.

**Tech Stack:** Python 3.12, SQLite, standard-library HTTP server, futu-api 10.10, pytest.

**Spec:** `docs/superpowers/specs/2026-09-02-paper-trading-operations-design.md`

## Global Constraints

- Only `TrdEnv.SIMULATE`, `TrdMarket.CN`, `SH.588080`, `TimeInForce.DAY`.
- Missing real-time quote permission is degraded, not blocking.
- Only cumulative order fill quantity and average price may update inferred fills.
- Runtime account data stays under ignored `state/paper_trading/`.
- No new research PASS thresholds.

---

### Task 1: Coordinator and durable state

**Files:**
- Create: `src/czsc_trader/paper_trading.py`
- Test: `tests/test_paper_trading.py`

**Interfaces:**
- Produces: `PaperStore`, `PaperCoordinator`, `PaperSnapshot`, `BrokerGateway`.
- Consumes: `run_advice(context, AdviceCommand)`.

- [ ] **Step 1: Write failing tests** for environment hard locks, pause semantics, idempotent order intent, cumulative fill reconciliation, and one-time cancel tokens using an in-memory broker.
- [ ] **Step 2: Run** `python -m pytest tests/test_paper_trading.py -q` and confirm missing-module failure.
- [ ] **Step 3: Implement** SQLite schema (`settings`, `events`, `orders`, `snapshots`), coordinator refresh, pause/resume, cancel-token issuance and confirmed cancellation.
- [ ] **Step 4: Run** the focused tests and confirm PASS.

### Task 2: Futu gateway

**Files:**
- Create: `src/czsc_trader/futu_gateway.py`
- Modify: `pyproject.toml`
- Test: `tests/test_futu_gateway.py`

**Interfaces:**
- Produces: `FutuGateway.snapshot()`, `place_limit_order()`, `cancel_order()`.
- Consumes: `BrokerAccount`, `BrokerOrder` value objects from Task 1.

- [ ] **Step 1: Write failing contract tests** with a fake SDK context proving SIMULATE/CN/DAY/whitelist arguments and cumulative order mapping.
- [ ] **Step 2: Run** the focused test and confirm the adapter is absent.
- [ ] **Step 3: Implement** lazy SDK imports, account selection, account/position/order queries, hard-locked order placement and cancellation.
- [ ] **Step 4: Add** `futu-api==10.10.7008` to runtime dependencies and rerun focused tests.

### Task 3: Local dashboard and CLI

**Files:**
- Create: `src/czsc_trader/paper_web.py`
- Create: `src/czsc_trader/application/paper_service.py`
- Modify: `src/czsc_trader/cli/main.py`
- Modify: `README.md`
- Test: `tests/test_paper_web.py`

**Interfaces:**
- Produces: `serve_dashboard(coordinator, host, port, interval)` and CLI `paper serve`.
- Consumes: Tasks 1 and 2.

- [ ] **Step 1: Write failing HTTP tests** for `/api/status`, `/api/pause`, `/api/resume`, `/api/cancel-token`, and confirmed `/api/cancel`.
- [ ] **Step 2: Run** the focused test and confirm missing endpoint behavior.
- [ ] **Step 3: Implement** JSON endpoints, a dependency-free HTML dashboard, background refresh loop, localhost binding and CLI arguments.
- [ ] **Step 4: Document** startup, runtime database, automatic submission and intervention behavior.
- [ ] **Step 5: Run** the three focused test files and Ruff on the new modules.

### Task 4: Start and verify locally

**Files:**
- Runtime only: `state/paper_trading/runtime.db`

**Interfaces:**
- Consumes: `paper serve`.
- Produces: running localhost dashboard.

- [ ] **Step 1: Run** existing `advice run` for `actual_position=0`, confirm the 2026-09-01 signal is aligned at cash and requires no order.
- [ ] **Step 2: Start** `paper serve --quantity 50000` in a hidden background process.
- [ ] **Step 3: Query** `/api/status`, confirm SIMULATE/CN/SH.588080, successful reconciliation and no new order.
- [ ] **Step 4: Open** the localhost dashboard in Codex and report the URL and process state.
