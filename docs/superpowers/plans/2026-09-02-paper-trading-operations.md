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

- [ ] Add failing tests for quantity deltas, numeric BUY/SELL DAY limits, deterministic IDs, and legacy CLI compatibility.
- [ ] Run focused tests and confirm v1-related failures.
- [ ] Implement validation, v1 serialization, and configured project-side sell protection pricing.
- [ ] Run focused tests and commit `feat: publish paper trading advice contract`.

### Task 2: Create PTE contracts and CLI client

**Files:** package `pyproject.toml`, `__init__.py`, `contracts.py`, `advice_client.py`, and `test_advice_client.py`.

**Interfaces:** Produces `AdviceDecision.from_payload()` and `CliAdviceClient.get_decision(actual_quantity)`; consumes exactly one stdout JSON document.

- [ ] Add failing tests for parsing, versions, limits, subprocess arguments, timeout, nonzero exit, and noisy stdout.
- [ ] Run focused tests and confirm missing-package failure.
- [ ] Implement immutable values and a shell-free UTF-8 subprocess client with explicit repository root.
- [ ] Run focused tests and commit `feat: add paper trading advice client`.

### Task 3: Add durable store and engine

**Files:** `store.py`, `engine.py`, and `test_engine.py`.

**Interfaces:** Produces `PaperStore`, `PaperTradingEngine.refresh()`, `pause()`, `resume()`, `issue_cancel_token()`, and `confirm_cancel()`; consumes advice and broker protocols.

- [ ] Add failing tests for locks, intent-before-submit, idempotency, monotonic fills, pause, gated resume, cancel tokens, and reopen recovery.
- [ ] Run focused tests and confirm missing behavior.
- [ ] Implement SQLite transactions, audit events, and the engine state machine.
- [ ] Run focused tests and commit `feat: add restart safe paper trading engine`.

### Task 4: Add Futu SIMULATE gateway

**Files:** `futu_gateway.py` and `test_futu_gateway.py`.

**Interfaces:** Produces `FutuGateway.snapshot()`, `place_order(intent)`, and `cancel_order(channel_order_id)`; consumes lazy-loaded Futu SDK and generic values.

- [ ] Add failing fake-context tests for SIMULATE/CN, symbol/lot/DAY, `adjust_limit=0`, remark, normalization, and cancellation.
- [ ] Run focused tests and confirm missing adapter.
- [ ] Implement lazy SDK access and stable gateway errors.
- [ ] Run focused tests and commit `feat: add futu paper gateway`.

### Task 5: Add local operations page and CLI

**Files:** `web.py`, `cli.py`, package README, `.gitignore`, `test_web.py`, and `test_cli.py`.

**Interfaces:** Produces `pte once`, `pte serve`, `/api/status`, `/api/pause`, `/api/resume`, `/api/cancel-token`, `/api/cancel`.

- [ ] Add failing HTTP/CLI tests for status, interventions, localhost defaults, and one-cycle execution.
- [ ] Run focused tests and confirm missing entry points.
- [ ] Implement the JSON API, dashboard, reconciliation loop, shutdown, and CLI wiring.
- [ ] Document installation, operation, degraded quote behavior, and recovery.
- [ ] Run focused tests and commit `feat: add paper trading operations console`.

### Task 6: Verify P0 integration

**Files:** Modify only when verification exposes a defect reproduced by a new failing test.

**Interfaces:** Consumes the complete stack and produces the final verification record.

- [ ] Install the independent package editable and run all PTE tests.
- [ ] Run root non-archive tests and Ruff for both packages.
- [ ] Validate a live `advice.v1` result with current simulated quantity.
- [ ] Run `pte once` against Futu only when the current decision requires no order; never submit a diagnostic order.
- [ ] Start `pte serve`, query status, and inspect the page.
- [ ] Review the spec, diff, and commits before handoff.
