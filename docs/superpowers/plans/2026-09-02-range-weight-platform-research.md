# Range Weight Platform Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute `0902_EX04` to locate connected range-weight platforms using maximum drawdown, Calmar, and win/loss ratio before a locked 2026 test.

**Architecture:** Add small reusable research primitives for exact group-share projection, Pareto layers, and simplex connectivity. A self-contained EX04 runner evaluates 261 frozen candidates over four research windows, freezes platform evidence, then evaluates 2026 once without changing the baseline.

**Tech Stack:** Python 3.12, pandas, NumPy, existing `czsc_trader` factors/regimes/backtests/archive validation, pytest.

**Spec:** `experiments/0902_EX04/02_design.md`

## Global Constraints

- Only range-state group shares vary; all trend-state scores must remain identical.
- Core comparison metrics are maximum drawdown, Calmar, and win/loss ratio with no return-based dominance.
- Research windows end on 2025-12-31; 2026 is inaccessible until candidates and platforms are frozen.
- No composite PASS gate and no automatic promotion.

---

### Task 1: Platform research primitives

**Files:**
- Create: `src/czsc_trader/range_platform.py`
- Create: `tests/test_range_weight_platform.py`

- [x] Write failing tests for exact group-share projection, Pareto layers, and simplex connected components.
- [x] Run the focused test and confirm failures caused by missing implementation.
- [x] Implement the three pure primitives with deterministic ordering and finite-value validation.
- [x] Run focused tests and confirm PASS.

### Task 2: EX04 frozen research and test runner

**Files:**
- Create: `experiments/0902_EX04/run_experiment.py`
- Create: `experiments/0902_EX04/artifacts/protocol.json`

- [x] Validate the exact 260-point simplex plus one active control.
- [x] Evaluate four 2021—2025 research windows and full-period range diagnostics.
- [x] Calculate three-metric Pareto layers and connected research platform components.
- [x] Freeze candidates, research metrics, and platform membership before loading 2026.
- [x] Evaluate all frozen candidates once on 2026 and record platform-member test outcomes.
- [x] Write identity, phase-order, active-control, and trend-score invariance audits.

### Task 3: Archive and handoff

**Files:**
- Modify: `tests/test_identity_and_archives.py`
- Create: `experiments/0902_EX04/03_execution.md`
- Create: `experiments/0902_EX04/04_conclusion.md`
- Create: `experiments/0902_EX04/experiment_manifest.json`
- Modify: `docs/RESEARCH_HANDOFF.md`

- [x] Generate complete machine-readable metrics, platform tables, execution notes, and conclusions.
- [x] Build and validate the EX04 experiment manifest.
- [x] Update the archive-count regression and research handoff.
- [x] Run focused tests, full ordinary tests, archive tests, archive CLI validation, compile checks, and `git diff --check`.
- [x] Commit EX04 on the current research branch without merging or pushing.
