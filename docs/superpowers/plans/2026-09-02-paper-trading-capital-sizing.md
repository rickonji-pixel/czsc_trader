# Paper Trading Full-Cash Sizing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Size each simulated entry from all currently deployable account cash while retaining project-owned price, fee, lot, and decision identity rules.

**Architecture:** Upgrade the strategy boundary to `advice.v2`: PTE passes reconciled cash and holdings, while `czsc_trader` computes the limit price first and derives the maximum fee-covered 100-share quantity. Futu remains an execution-only adapter and never changes price or quantity.

**Tech Stack:** Python 3.12, subprocess JSON contract, dataclasses, Decimal/explicit rounding, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-02-paper-trading-operations-design.md`

## Global Constraints

- Execute after the runtime-hardening plan so submissions already have a valid trading-time gate.
- Use reconciled simulated-account cash only; do not use margin, short buying power, or unrealized assets.
- Reserve the configured `fee_rate` before rounding down to a 100-share lot.
- Keep all limit-price calculation inside `czsc_trader`; Futu receives exact project price and quantity with `adjust_limit=0`.
- Keep advice v1 available for explicit legacy callers during migration; PTE consumes v2 exclusively after cutover.
- A non-positive affordable quantity produces WAIT plus `INSUFFICIENT_BUYING_POWER`, never a zero-quantity order.

---

### Task 1: Publish `advice.v2` full-cash sizing

**Files:**
- Modify: `src/czsc_trader/application/advice_service.py`
- Modify: `src/czsc_trader/cli/main.py`
- Test: `tests/test_execution_policy.py`
- Test: `tests/test_repository_contract.py`

**Interfaces:**
- Produces: `AdviceCommand(actual_quantity: int, available_cash: Decimal)` and `advice.v2`.
- Consumes: execution-policy `fee_rate`, project limit price, and 100-share lot size.

- [ ] **Step 1: Add exact sizing tests**

For BUY, assert
`floor(available_cash / (limit_price * (1 + fee_rate)) / 100) * 100` shares; test exact boundary,
one cent below boundary, less than one lot, non-finite/negative cash, and SELL of all actual shares.
When the strategy target is long, define `target_quantity = actual_quantity + affordable_quantity`;
when it is cash, define `target_quantity = 0`.

- [ ] **Step 2: Add deterministic identity tests**

Canonicalize cash as decimal currency units with two fractional digits before hashing. Assert equal
economic cash values produce one `decision_id`; a cash change that crosses a 100-share affordability
boundary produces a different order and ID.

- [ ] **Step 3: Run focused tests and verify v2 failures**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_execution_policy.py tests/test_repository_contract.py -q`

- [ ] **Step 4: Implement v2 without floating-point affordability errors**

Compute with `Decimal(str(value))`; calculate price first, include `fee_rate`, round shares down to
100, and emit `available_cash`, `fee_rate`, `estimated_order_cost`, and `unallocated_cash` for audit.

- [ ] **Step 5: Run tests and commit**

Commit: `git commit -am "feat: publish full-cash advice v2"`

### Task 2: Upgrade the PTE advice client contract

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/contracts.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/advice_client.py`
- Test: `packages/paper_trading_engine/tests/test_advice_client.py`

**Interfaces:**
- Produces: `CliAdviceClient.get_decision(actual_quantity: int, available_cash: Decimal) -> AdviceDecision`.
- Consumes: exactly one successful `advice.v2` JSON document.

- [ ] **Step 1: Add parser and subprocess tests**

Require v2 audit fields, numeric finite non-negative cash, fee-covered order cost, 100-share lots,
and `--available-cash`; reject v1 in the PTE automatic path after cutover.

- [ ] **Step 2: Run tests and verify contract failures**

- [ ] **Step 3: Implement immutable v2 values and shell-free CLI arguments**

Serialize cash using fixed decimal text. Keep timeout, UTF-8, one-document stdout, explicit data
directory, and no-shell rules unchanged.

- [ ] **Step 4: Run tests and commit**

Commit: `git commit -am "feat: consume full-cash advice contract"`

### Task 3: Feed reconciled cash into decisions

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/engine.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/cli.py`
- Test: `packages/paper_trading_engine/tests/test_engine.py`
- Test: `packages/paper_trading_engine/tests/test_cli.py`

**Interfaces:**
- Produces: decision cache key `(data_identity, actual_quantity, available_cash)`.
- Consumes: `BrokerAccount.cash` and v2 advice client.

- [ ] **Step 1: Add engine tests for cash changes and partial fills**

Assert unchanged cash avoids CLI reruns; changed reconciled cash reruns advice; active orders still
block replacement; after terminal partial fill/cancel, remaining cash can create one new decision;
SELL always uses the full actual quantity.

- [ ] **Step 2: Remove required fixed `--position-size` from PTE automatic commands**

Keep an explicitly named legacy option only where v1 compatibility tests need it. Service config
must use `allocation_mode="all_available_cash"`.

- [ ] **Step 3: Implement the new cache and validation**

Reject advice when returned actual quantity or cash identity differs from the reconciled snapshot.
Preserve symbol, environment, market, order, and idempotency checks.

- [ ] **Step 4: Run tests and commit**

Commit: `git commit -am "feat: size paper orders from reconciled cash"`

### Task 4: Expose allocation evidence and verify end to end

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/web.py`
- Modify: `packages/paper_trading_engine/README.md`
- Test: `packages/paper_trading_engine/tests/test_web.py`

**Interfaces:**
- Produces: Chinese allocation evidence in the decision card and formatted diagnostics.
- Consumes: v2 cash, fee, estimated cost, order quantity, and residual cash fields.

- [ ] **Step 1: Add UI assertions**

Require 可用资金、费率、预计占用资金、委托数量、预计剩余资金 and allocation mode. Keep
raw v2 JSON only in the expandable diagnostic section.

- [ ] **Step 2: Run the full automated suite**

Run PTE tests, root tests, Ruff, and `git diff --check`.

- [ ] **Step 3: Perform a read-only live sizing check**

Query the simulated account and run `czsc-trader advice run` without starting automatic submission.
Verify the formula from returned cash, limit, fee, lot, estimated cost, and remainder. Do not infer a
fill and do not submit a probe order.

- [ ] **Step 4: Start the service outside the submission window and inspect status**

Confirm the service shows the v2 decision, remains outside the submission gate, and generates no
new intent. Record the exact observed values in the delivery note.

- [ ] **Step 5: Commit documentation and verification evidence**

Commit: `git commit -am "docs: verify full-cash paper sizing"`
