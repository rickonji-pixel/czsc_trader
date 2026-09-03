# SM SE PTE Functional Test Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace SM, SE, and PTE fine-grained tests with 13 deterministic end-to-end functional scenarios.

**Architecture:** Each package receives a `tests/functional` suite that enters through its public class, orchestration service, CLI, HTTP, or browser-state boundary. Existing test data builders may be consolidated into non-collected support modules; all old `test_*.py` files are deleted only after replacement scenarios pass.

**Tech Stack:** Python 3.12, pytest, SQLite, standard-library HTTP server, Node test runner, existing SM/SE/PTE APIs.

**Spec:** `docs/superpowers/specs/2026-09-04-sm-se-pte-functional-test-consolidation-design.md`

## Global Constraints

- Implement exactly 2 SM, 3 SE, 7 PTE Python, and 1 PTE JavaScript functional scenarios.
- Preserve production code and all trading safety boundaries.
- Use deterministic local fakes for vendors, broker, clock, process, HTTP, and Windows service operations.
- Delete old tests only after the replacement suite passes.
- Execute inline in the current branch without worktrees or subagents.

---

### Task 1: Consolidate Strategy Manager

**Files:** Create `packages/strategy_manager/tests/functional/test_strategy_manager.py`; modify its `pyproject.toml`; delete the three old Python test files and old conftest.

- [ ] Build valid Strategy, StrategyVersion, and PerformanceEvidence values inside the new module.
- [ ] Add `test_ft_sm01_complete_lifecycle_is_persistent_and_auditable` covering create through retire and registry reopen.
- [ ] Add `test_ft_sm02_governance_rejects_invalid_mutation_and_tampering` covering strict models, atomic failures, continuity, evidence, and immutable release hash.
- [ ] Run the two tests, switch `testpaths` to `tests/functional`, delete old tests, rerun, and commit.

### Task 2: Consolidate Strategy Evaluator

**Files:** Create `packages/strategy_evaluator/tests/functional/test_strategy_evaluator.py`; modify its `pyproject.toml`; delete the twelve old Python test files.

- [ ] Build one fixed protocol, candidate pool, metric observations, return matrices, parameter points, execution evidence, stress results, and trial ledger.
- [ ] Add `test_ft_se01_candidate_funnel_selects_the_robust_pareto_champion` and assert the complete shortlist/ranking/final decision.
- [ ] Add `test_ft_se02_champion_audit_combines_statistical_and_engineering_evidence` and assert search-bias, Bootstrap, neighborhood, execution, stress, reproducibility, status, and risk label outputs.
- [ ] Add `test_ft_se03_governance_validation_and_report_are_complete` and assert immutable models, cannot-loosen margins, invalid evidence rejection, insufficient result, serialization, and report sections.
- [ ] Run the three tests, switch `testpaths`, delete old tests, rerun, and commit.

### Task 3: Consolidate PTE Accounts and Engine

**Files:** Create PTE functional conftest/support plus `test_accounts.py` and `test_engine.py`.

- [ ] Consolidate the existing SQLite store, fake advice, fake broker, fixed clock, strategy binding, and virtual-fill builders.
- [ ] Add FT-PTE01 for two isolated accounts, pause/resume, migration, and persistence.
- [ ] Add FT-PTE02 for decision, intent, submit, partial/full fill, exit, restart idempotence, and split orders.
- [ ] Run both scenarios and commit.

### Task 4: Consolidate PTE Channel, Scheduler, and Performance

**Files:** Create `test_channel.py`, `test_scheduler.py`, and `test_performance.py`.

- [ ] Add FT-PTE03 with an SDK fake proving SIMULATE lock, whitelist/lot validation, binding, and stored ownership.
- [ ] Add FT-PTE04 with fixed Beijing clocks proving 19:00 publication, backoff, trading window, overnight decision execution, and event recovery.
- [ ] Add FT-PTE07 exporting one self-contained strategy-bound performance bundle from a deterministic ledger.
- [ ] Run the three scenarios and commit.

### Task 5: Consolidate PTE Web, Watchdog, and JavaScript

**Files:** Create `test_web.py`, `test_watchdog.py`, and `console_state.test.mjs` under PTE functional tests.

- [ ] Add FT-PTE05 by starting one local HTTP server and exercising account resources, audit lists, interventions, system events, and token-protected restart.
- [ ] Add FT-PTE06 with fake process/HTTP/service runners covering health failure threshold, backoff, port probe, auto-start, and recovery commands.
- [ ] Merge all browser-state assertions into `test_ft_ptejs01_console_state_supports_multi_account_operations`.
- [ ] Run the two Python and one JavaScript scenarios and commit.

### Task 6: Remove Old PTE Tests and Complete Repository Verification

**Files:** Modify PTE `pyproject.toml`, README, and development handoff; delete old PTE Python and JavaScript test files.

- [ ] Prove all seven PTE Python scenarios and the JavaScript scenario pass.
- [ ] Switch PTE `testpaths` to `tests/functional`, delete old tests, and update commands/documentation.
- [ ] Run exact collection counts for Trader, SM, SE, and PTE.
- [ ] Run all four Python functional suites together, PTE JavaScript, 56 archive validations, Ruff, compileall, pip check, and diff check.
- [ ] Confirm configs, experiments, and state are unchanged, then commit the completed consolidation.
