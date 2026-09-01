# EX04 Decision Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Determine whether a simple, pre-known, cross-year-stable condition identifies when following the registered general baseline's entry/hold action adds net value over the frozen `0824_EX04` strategy on 2021-2025 data.

**Architecture:** Reuse the Git-tracked `0825_EX03` daily path ledger and decision-event archive instead of rerunning strategies. Convert each baseline-long/EX04-cash divergence interval into one fee-inclusive action-value observation, join it one-to-one to the causal decision event, then perform leave-one-year-out selection over a frozen library of simple boundary rules. The experiment is diagnostic only: it cannot read 2026, optimize a strategy, write orders, or freeze a challenger.

**Tech Stack:** Python 3.12, pandas, NumPy, pytest, existing `scripts/run_experiment.py` archive workflow.

**Spec:** `experiments/0826_EX01/02_design.md`

## Global Constraints

- Current research baseline is `experiments/0824_EX04/artifacts/frozen_challenger.json`; it is not the registered general baseline.
- Visible evidence stops at `2025-12-31`; no 2026 file, hash, metric, or prior holdout result may enter the runner.
- Source facts come only from Git-tracked experiment artifacts with preregistered byte hashes.
- The unit of evidence is one contiguous interval where the general baseline executes long and `0824_EX04` executes cash.
- The alternative action value is the negative sum of `0825_EX03`'s `log_wealth_delta` over that interval, so positive values favor deviating from EX04.
- Rule selection is nested leave-one-year-out across 2021-2025 and uses no held-out-year outcomes.
- The experiment produces no candidate, order file, frozen strategy, holdout metrics, or baseline update.
- Formal execution occurs once through `scripts/run_experiment.py` after the preregistration and implementation commit.

---

### Task 1: Freeze the research archive

**Files:**
- Create: `experiments/0826_EX01/01_goal.md`
- Create: `experiments/0826_EX01/02_design.md`
- Create: `experiments/0826_EX01/implementation_plan.md`
- Create: `experiments/0826_EX01/03_execution.md`
- Create: `experiments/0826_EX01/04_conclusion.md`
- Create: `experiments/0826_EX01/artifacts/protocol.json`

**Interfaces:**
- Consumes: byte identities for the EX04 frozen strategy and the `0825_EX03` protocol, daily ledger, decision events, and experiment manifest.
- Produces: a `PRE_REGISTERED` protocol with fixed source hashes, rule domains, support gates, fold ranking, and promotion-disabled fields.

- [ ] **Step 1: Write the goal and falsifiable conclusion boundary**

State that only `stable_boundary_found` permits a later independent strategy-design experiment; every other complete classification retains EX04 without modification.

- [ ] **Step 2: Write the causal sample and validation design**

Define interval construction, the one unmatched period-initial interval exclusion, action-value sign, the fixed rule library, leave-one-year-out selection, annual sign test, and concentration gate.

- [ ] **Step 3: Write the immutable protocol**

Set years to `2021` through `2025`, margin half-band to `0.0125`, allowed regimes to `uptrend/sideways/downtrend`, allowed block labels to the four existing literal labels, minimum training support to 8 events across all four training years, minimum out-of-fold support to 10 events across all five years, rule identity agreement to 4/5 folds, positive held-out years to 5/5, one-sided annual sign-test alpha to 0.05, and maximum single-event positive-gain share to 0.25.

- [ ] **Step 4: Validate the archive is still documentation-only**

Run: `git diff --check`
Expected: exit 0 with no whitespace errors.

### Task 2: Build the decision-frontier evidence table with TDD

**Files:**
- Create: `tests/test_decision_boundary_runner.py`
- Create: `src/czsc_trader/decision_boundary_runner.py`

**Interfaces:**
- Consumes: `daily_path_ledger.csv`, `decision_events.csv`, and the preregistered protocol.
- Produces: `validate_protocol(protocol) -> None`, `build_frontier_events(ledger, decision_events, protocol) -> tuple[pd.DataFrame, pd.DataFrame]`, and `enumerate_boundary_rules(protocol) -> pd.DataFrame`.

- [ ] **Step 1: Write failing tests for protocol isolation and interval construction**

Tests must prove that 2026 access and promotion are rejected; contiguous baseline-long/EX04-cash days become one interval; action value uses the correct sign; a period-initial interval without a causal event is excluded and audited; duplicated or unmatched non-initial events fail.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/test_decision_boundary_runner.py -q`
Expected: collection failure because `czsc_trader.decision_boundary_runner` does not exist.

- [ ] **Step 3: Implement the minimal validated interval builder**

The builder must sort by window/date, identify contiguous `baseline_execution_position == 1` and `ex04_execution_position == 0` intervals, sum fee-inclusive log wealth differences, join the interval start to exactly one decision event execution date, derive `enter_now` or `continue_hold`, derive the fixed near/far margin band, and reject non-finite or future-dated evidence.

- [ ] **Step 4: Implement the deterministic rule library**

Generate the fallback `always_ex04` plus literal family, family+regime, family+block, family+margin, family+regime+margin, and family+block+margin rules. Rule IDs and complexity are deterministic and do not depend on outcome values.

- [ ] **Step 5: Run the focused tests and verify GREEN**

Run: `.venv/Scripts/python.exe -m pytest tests/test_decision_boundary_runner.py -q`
Expected: all focused tests pass.

### Task 3: Implement nested selection and machine classification with TDD

**Files:**
- Modify: `tests/test_decision_boundary_runner.py`
- Modify: `src/czsc_trader/decision_boundary_runner.py`

**Interfaces:**
- Produces: `select_training_rule(events, rules, held_out_year, protocol) -> dict[str, object]`, `run_leave_one_year_out(events, rules, protocol) -> tuple[pd.DataFrame, pd.DataFrame]`, and `classify_boundary(folds, predictions, protocol) -> dict[str, object]`.

- [ ] **Step 1: Write failing tests for training-only selection**

Use synthetic events where changing a held-out outcome cannot change that fold's selected rule, insufficient-support rules are ignored, and deterministic ties resolve by positive training years, worst annual mean, median annual mean, pooled mean, larger support, lower complexity, then rule ID.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/test_decision_boundary_runner.py -q`
Expected: failures because nested selection is absent.

- [ ] **Step 3: Implement minimal leave-one-year-out selection**

Each fold trains on four years, requires at least eight matching events with representation in all four training years, selects only rules with positive pooled training value, and emits held-out event-level predictions without reading held-out outcomes during selection.

- [ ] **Step 4: Write failing tests for the stability gates**

Cover a passing five-year synthetic boundary, rule-identity instability, one non-positive held-out year, inadequate support, annual sign-test failure, and excessive single-event positive-gain concentration.

- [ ] **Step 5: Implement the machine classifier**

Return `stable_boundary_found` only when every preregistered gate passes; otherwise return `no_stable_boundary` with each gate and measured statistic preserved.

- [ ] **Step 6: Run the focused tests and verify GREEN**

Run: `.venv/Scripts/python.exe -m pytest tests/test_decision_boundary_runner.py -q`
Expected: all focused tests pass.

### Task 4: Integrate the formal experiment entrypoint with TDD

**Files:**
- Modify: `tests/test_experiment_entrypoints.py`
- Modify: `scripts/run_experiment.py`
- Modify: `src/czsc_trader/decision_boundary_runner.py`

**Interfaces:**
- Produces: `run_decision_boundary_diagnosis(repository_root, experiment_dir, protocol) -> dict[str, object]` and `run_preregistered_decision_boundary(experiment_dir) -> Path`.

- [ ] **Step 1: Write the failing entrypoint test**

The test supplies a preregistered archive, stubs only the core runner, and asserts that the existing `--experiment-dir` dispatch writes execution/conclusion documents, manifest metadata, and no forbidden strategy artifacts.

- [ ] **Step 2: Run the entrypoint test and verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/test_experiment_entrypoints.py -q`
Expected: failure because `decision_boundary_diagnosis` is unsupported.

- [ ] **Step 3: Implement archive orchestration and artifact writing**

Validate every source byte hash before analysis; write `identity_audit.json`, `frontier_events.csv`, `excluded_intervals.csv`, `boundary_rules.csv`, `fold_selection.csv`, `out_of_fold_predictions.csv`, `boundary_classification.json`, and `metrics.json`; generate Chinese execution and conclusion documents from machine evidence; build and validate the experiment manifest.

- [ ] **Step 4: Run entrypoint and focused runner tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_decision_boundary_runner.py tests/test_experiment_entrypoints.py -q`
Expected: all selected tests pass.

### Task 5: Freeze implementation, execute once, and archive the result

**Files:**
- Modify: `experiments/0826_EX01/03_execution.md`
- Modify: `experiments/0826_EX01/04_conclusion.md`
- Create: `experiments/0826_EX01/experiment_manifest.json`
- Create: `experiments/0826_EX01/artifacts/identity_audit.json`
- Create: `experiments/0826_EX01/artifacts/frontier_events.csv`
- Create: `experiments/0826_EX01/artifacts/excluded_intervals.csv`
- Create: `experiments/0826_EX01/artifacts/boundary_rules.csv`
- Create: `experiments/0826_EX01/artifacts/fold_selection.csv`
- Create: `experiments/0826_EX01/artifacts/out_of_fold_predictions.csv`
- Create: `experiments/0826_EX01/artifacts/boundary_classification.json`
- Create: `experiments/0826_EX01/artifacts/metrics.json`

**Interfaces:**
- Consumes: the frozen implementation commit and `experiments/0826_EX01/artifacts/protocol.json`.
- Produces: one complete, Git-tracked `0826_EX01` research archive.

- [ ] **Step 1: Run the minimum regression suite**

Run: `.venv/Scripts/python.exe -m pytest tests/test_decision_boundary_runner.py tests/test_experiment_entrypoints.py tests/test_experiment_archive.py -q`
Expected: all selected tests pass.

- [ ] **Step 2: Commit the preregistration and implementation**

Run: `git add docs/superpowers/plans/2026-08-26-ex04-decision-boundary.md experiments/0826_EX01 src/czsc_trader/decision_boundary_runner.py scripts/run_experiment.py tests/test_decision_boundary_runner.py tests/test_experiment_entrypoints.py`

Run: `git commit -m "research: freeze 0826 EX01 decision boundary diagnosis"`

- [ ] **Step 3: Execute the preregistered experiment once**

Run: `.venv/Scripts/python.exe scripts/run_experiment.py --experiment-dir experiments/0826_EX01`
Expected: the command prints the actual completed experiment directory and reports no 2026 access.

- [ ] **Step 4: Validate the completed archive and focused regressions**

Run: `.venv/Scripts/python.exe -m pytest tests/test_decision_boundary_runner.py tests/test_experiment_entrypoints.py tests/test_experiment_archive.py -q`

Run: `git diff --check`

Run: `git status --short --branch`

Expected: tests pass, no whitespace errors, and only expected formal-result files are uncommitted.

- [ ] **Step 5: Commit the formal evidence**

Run: `git add experiments/0826_EX01`

Run: `git commit -m "research: archive 0826 EX01 decision boundary result"`
