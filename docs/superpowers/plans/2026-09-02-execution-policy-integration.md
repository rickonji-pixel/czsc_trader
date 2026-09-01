# Execution Policy Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote the frozen 588080 execution policy, expose daily manual advice, and add an execution-aware baseline row to supported backtests.

**Architecture:** Resolve execution policies from an immutable registry bound to the symbol and signal-baseline hash. Keep advice stateless with explicit actual position, and let the generic backtest add the execution-policy strategy only when identities match.

**Tech Stack:** Python 3.12, argparse, pandas, existing CZSC factor and baseline APIs, pytest.

**Spec:** `docs/superpowers/specs/2026-09-02-execution-policy-integration-design.md`

## Global Constraints

- Work in the current main session and branch; do not use worktrees or subagents.
- Preserve arbitrary-symbol backtest compatibility.
- Never infer fills or mutate holdings.
- Use latest complete locally tracked close data for advice.
- Keep tests in `tests/test_cli_e2e.py` and run only focused verification plus archive validation.

---

### Task 1: Immutable execution-policy registry

**Files:**
- Create: `configs/execution_policies/registry.json`
- Create: `configs/execution_policies/execution_policy_20260902.json`
- Create: `src/czsc_trader/execution_policies.py`
- Modify: `src/czsc_trader/application/context.py`
- Test: `tests/test_cli_e2e.py`

**Interfaces:**
- Produces: `ResolvedExecutionPolicy`.
- Produces: `resolve_execution_policy(root, version=None, symbol=None, baseline=None, required=False)`.

- [ ] Write a failing identity-resolution test.
- [ ] Run it and confirm the module/config is missing.
- [ ] Freeze the policy and implement exact hash/source/baseline verification.
- [ ] Run the focused test and commit.

### Task 2: Stateless daily advice service and CLI

**Files:**
- Create: `src/czsc_trader/application/advice_service.py`
- Modify: `src/czsc_trader/cli/main.py`
- Test: `tests/test_cli_e2e.py`

**Interfaces:**
- Produces: `AdviceCommand(symbol, asset_type, actual_position, quantity, baseline=None)`.
- Produces: `run_advice(context, request) -> CommandResult`.
- Produces CLI: `czsc-trader advice run`.

- [ ] Write failing tests for the four state/action combinations and exact buy prices.
- [ ] Run them and confirm the command is unavailable.
- [ ] Implement the service using the existing validated data, factor, and baseline paths.
- [ ] Add validation for position, quantity, and policy identity.
- [ ] Run focused advice tests and commit.

### Task 3: Execution-aware backtest comparison

**Files:**
- Modify: `src/czsc_trader/backtest_runner.py`
- Modify: `src/czsc_trader/application/backtest_service.py`
- Test: `tests/test_cli_e2e.py`

**Interfaces:**
- `BacktestRequest` receives `execution_policy_root`.
- Matching runs add strategy key `active_baseline_execution_policy`.
- Matching runs write `execution_orders*.csv` and `execution_equity*.csv`.

- [ ] Extend the existing backtest test first and confirm it fails on the missing fourth strategy.
- [ ] Simulate the frozen limit policy independently for each requested window.
- [ ] Add the fourth metric/report row and manifest identity.
- [ ] Run the focused backtest test and commit.

### Task 4: Documentation and final verification

**Files:**
- Modify: `docs/RESEARCH_HANDOFF.md`
- Modify: `README.md` if command usage is documented there.

- [ ] Document the promoted policy, advice command, explicit-position rule, and dual backtest interpretation.
- [ ] Run focused advice, execution-policy, baseline, backtest, and archive tests.
- [ ] Run `compileall`, `git diff --check`, and all archive validation.
- [ ] Commit the completed integration and report the branch state without merging or pushing unless requested.
