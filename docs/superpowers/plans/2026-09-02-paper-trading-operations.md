# Paper Trading Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a broker-neutral, restart-safe paper trading engine and localhost operations page for `SH.588080`, with Futu SIMULATE as the first channel.

**Architecture:** `paper-trading-engine` is an independent package beside `czsc_trader`. It consumes a versioned JSON contract from the `czsc-trader advice run` CLI, owns runtime orchestration and SQLite audit state, and delegates execution to a hard-locked broker adapter.

**Tech Stack:** Python 3.12, SQLite, standard-library HTTP server, futu-api 10.10.7008, pytest.

**Spec:** `docs/superpowers/specs/2026-09-02-paper-trading-operations-design.md`

## Global Constraints

- Development runs on branch `codex/paper-trading-engine` in the existing checkout; no worktree.
- Only paper environment, China market, `SH.588080`, DAY limit orders, and 100-share lots.
- Pricing is owned by `czsc_trader`; broker-side automatic price adjustment is disabled.
- Missing real-time quote permission is degraded and does not block submission.
- Only explicit cumulative fill quantity creates fill increments.
- Runtime data remains under ignored `state/paper_trading/`.
- P0 only; no sample-out performance comparison or new research acceptance gate.

---

### Task 1: Publish advice.v1

**Files:** `src/czsc_trader/application/advice_service.py`, `src/czsc_trader/cli/main.py`, execution-policy config/registry, root README, and advice tests.

**Interfaces:** Produces `AdviceCommand(actual_quantity, position_size)` and `advice.v1`; consumes validated prices and frozen identities.

- [x] Add failing tests for quantity deltas, numeric BUY/SELL DAY limits, deterministic IDs, and legacy CLI compatibility.
- [x] Run focused tests and confirm v1-related failures.
- [x] Implement validation, v1 serialization, and configured project-side sell protection pricing.
- [x] Run focused tests and commit `feat: publish paper trading advice contract`.

### Task 2: Create PTE contracts and CLI client

**Files:** package `pyproject.toml`, `__init__.py`, `contracts.py`, `advice_client.py`, and `test_advice_client.py`.

**Interfaces:** Produces `AdviceDecision.from_payload()` and `CliAdviceClient.get_decision(actual_quantity)`; consumes exactly one stdout JSON document.

- [x] Add failing tests for parsing, versions, limits, subprocess arguments, timeout, nonzero exit, and noisy stdout.
- [x] Run focused tests and confirm missing-package failure.
- [x] Implement immutable values and a shell-free UTF-8 subprocess client with explicit repository root.
- [x] Run focused tests and commit `feat: add paper trading advice client`.

### Task 3: Add durable store and engine

**Files:** `store.py`, `engine.py`, and `test_engine.py`.

**Interfaces:** Produces `PaperStore`, `PaperTradingEngine.refresh()`, `pause()`, `resume()`, `issue_cancel_token()`, and `confirm_cancel()`; consumes advice and broker protocols.

- [x] Add failing tests for locks, intent-before-submit, idempotency, monotonic fills, pause, gated resume, cancel tokens, and reopen recovery.
- [x] Run focused tests and confirm missing behavior.
- [x] Implement SQLite transactions, audit events, and the engine state machine.
- [x] Run focused tests and commit `feat: add restart safe paper trading engine`.

### Task 4: Add Futu SIMULATE gateway

**Files:** `futu_gateway.py` and `test_futu_gateway.py`.

**Interfaces:** Produces `FutuGateway.snapshot()`, `place_order(intent)`, and `cancel_order(channel_order_id)`; consumes lazy-loaded Futu SDK and generic values.

- [x] Add failing fake-context tests for SIMULATE/CN, symbol/lot/DAY, `adjust_limit=0`, remark, normalization, and cancellation.
- [x] Run focused tests and confirm missing adapter.
- [x] Implement lazy SDK access and stable gateway errors.
- [x] Run focused tests and commit `feat: add futu paper gateway`.

### Task 5: Add local operations page and CLI

**Files:** `web.py`, `cli.py`, package README, `.gitignore`, `test_web.py`, and `test_cli.py`.

**Interfaces:** Produces `pte once`, `pte serve`, `/api/status`, `/api/pause`, `/api/resume`, `/api/cancel-token`, `/api/cancel`.

- [x] Add failing HTTP/CLI tests for status, interventions, localhost defaults, and one-cycle execution.
- [x] Run focused tests and confirm missing entry points.
- [x] Implement the JSON API, dashboard, reconciliation loop, shutdown, and CLI wiring.
- [x] Document installation, operation, degraded quote behavior, and recovery.
- [x] Run focused tests and commit `feat: add paper trading operations console`.

### Task 6: Verify P0 integration

**Files:** Modify only when verification exposes a defect reproduced by a new failing test.

**Interfaces:** Consumes the complete stack and produces the final verification record.

- [x] Install the independent package editable and run all PTE tests.
- [x] Run root non-archive tests and Ruff for both packages.
- [x] Validate a live `advice.v1` result with current simulated quantity.
- [x] Run `pte once` against Futu only when the current decision requires no order; never submit a diagnostic order.
- [x] Start `pte serve`, query status, and inspect the page endpoint.
- [x] Review the spec, diff, and commits before handoff.

### Task 7: Split observation and decision schedules

- [x] Poll order status and cumulative fills about every 5 seconds.
- [x] Refresh account and positions every 30 to 60 seconds.
- [x] Invoke `czsc-trader advice run` only after complete-close data identity or actual holdings change.
- [x] Cache the observed decision and re-evaluate its eligibility without rerunning the daily strategy.

### Task 8: Complete UI and intervention corrections

- [x] Add a presentation-only Chinese label map for environment, health, run state,
  decision action, order status, and alerts while preserving stable English contracts.
- [x] Replace raw account, decision, order, and event JSON blocks with semantic cards,
  tables, and expandable formatted details.
- [x] Reproduce the inactive Resume button from a paused state and add an interaction
  regression test covering request, response, engine state, refresh, and visible feedback.
- [x] Make Resume success and failure visible without relying on background polling.

### Task 9: Publish complete-close runtime data

- [x] Keep mutable runtime data under ignored `state/paper_trading/data`.
- [x] Publish data once after 16:15 and record successful publication by date.
- [x] Record failures as audit alerts and retry with bounded backoff.

### Task 10: Use the exchange trading calendar

- [x] Resolve the next open session from the Tushare SSE calendar during data publication.
- [x] Persist that session in the execution-data manifest.
- [x] Require advice generation to consume the persisted session instead of inferring weekdays.
