# CZSC Factor Stability Diagnostic Implementation Plan

> **For agentic workers:** Execute in the main session only. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discover whether newly exposed CZSC structural states or sparse events contain stable 20-day return or drawdown information for 588080.

**Architecture:** Pure diagnostic functions build causal forward outcomes and compare active versus inactive samples by year. A registered experiment runner freezes discovery candidates before loading locked validation data and writes a standard auditable experiment archive.

**Tech Stack:** Python 3.12, pandas, NumPy, existing CZSC signal generator and experiment registry.

**Spec:** `experiments/0901_EX01/02_design.md`

## Global Constraints

- Never load data after 2025-12-31.
- Discovery is 2021-2023; validation is 2024-2025.
- The primary horizon is exactly 20 trading days.
- No strategy, weight, threshold, or position optimization occurs in this experiment.
- State and event factors use separate support checks.

---

### Task 1: Pure stability diagnostics

**Files:**
- Create: `src/czsc_trader/czsc_factor_stability.py`
- Test: `tests/test_czsc_factor_stability.py`

**Interfaces:**
- Produces: `build_forward_outcomes`, `evaluate_factor_stability`, `select_discovery_candidates`, and `validate_frozen_candidates`.

- [ ] Write focused tests for causal horizon labels, event-transition collapsing, yearly sign consistency, and validation direction locking.
- [ ] Run the focused tests and verify they fail because the module does not exist.
- [ ] Implement the smallest pure functions that satisfy the protocol.
- [ ] Run the focused tests and verify they pass.

### Task 2: Registered experiment runner

**Files:**
- Create: `src/czsc_trader/czsc_factor_stability_runner.py`
- Modify: `src/czsc_trader/research/handlers.py`
- Modify: `tests/test_czsc_factor_stability.py`

**Interfaces:**
- Consumes: the Task 1 functions and existing `generate_candidate_factors`.
- Produces: `run_czsc_factor_stability_experiment` and handler id `czsc_factor_stability_diagnostic`.

- [ ] Add a failing registry-resolution test for the exact protocol.
- [ ] Register the new handler and implement discovery-before-validation execution.
- [ ] Run focused tests and confirm handler resolution and cutoff protection pass.

### Task 3: Execute and archive 0901_EX01

**Files:**
- Create: `experiments/0901_EX01/artifacts/*.csv`
- Create: `experiments/0901_EX01/artifacts/*.json`
- Create: `experiments/0901_EX01/03_execution.md`
- Create: `experiments/0901_EX01/04_conclusion.md`
- Create: `experiments/0901_EX01/experiment_manifest.json`

- [ ] Commit the pre-registered protocol before execution.
- [ ] Run `czsc-trader experiment run --dir experiments/0901_EX01`.
- [ ] Inspect candidate support, discovery selection, and locked validation results.
- [ ] Validate the experiment archive and run only the focused tests plus CLI contract smoke coverage.
- [ ] Commit implementation and complete archive without merging or pushing.

