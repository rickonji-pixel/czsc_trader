# Range Weight Feasibility Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and execute `0902_EX03` to test whether changing only range-state group weights improves choppy-market trades without using 2026 for tuning.

**Architecture:** Keep all reusable trading primitives unchanged and implement a self-contained experiment runner under `experiments/0902_EX03`. The runner performs a 25-candidate 2021—2025 research pass, freezes identities and representative selections, then loads and evaluates the locked 2026 test exactly once.

**Tech Stack:** Python 3.12, pandas, NumPy, existing `czsc_trader` factor/regime/backtest/archive primitives, pytest.

**Spec:** `experiments/0902_EX03/02_design.md`

## Global Constraints

- Active baseline `baseline_20260901` and its registry remain unchanged.
- Only range trend-group and volume-position-group multipliers vary over `[0.50, 0.75, 1.00, 1.25, 1.50]`.
- Trend weights, regime classification, state machine, fees, and next-session-open execution remain frozen.
- 2021—2025 is the research sample; 2026-01-01 through 2026-09-02 is loaded only after candidates are frozen.
- No PASS/FAIL gate and no automatic baseline promotion.

---

### Task 1: Pure diagnostic and selection helpers

**Files:**
- Create: `experiments/0902_EX03/run_experiment.py`
- Create: `tests/test_range_weight_research.py`

**Interfaces:**
- Produces: `classify_trade_cycles(orders, regimes, sessions) -> pandas.DataFrame`
- Produces: `summarize_candidate(candidate_id, multipliers, result, regimes, sessions, init_cash) -> dict`
- Produces: `select_representatives(metrics) -> dict[str, int]`

- [x] Write focused tests for trade classification, short-loss counting, compounding, and deterministic representative selection.
- [x] Run `pytest tests/test_range_weight_research.py -q` and confirm the tests initially fail because the runner is absent.
- [x] Implement the smallest pure helper functions needed by the tests.
- [x] Run `pytest tests/test_range_weight_research.py -q` and confirm PASS.

### Task 2: Frozen two-stage experiment runner

**Files:**
- Modify: `experiments/0902_EX03/run_experiment.py`
- Create: `experiments/0902_EX03/artifacts/protocol.json`

**Interfaces:**
- Consumes: active and parent baseline identities, validated raw data, existing factor/regime/backtest functions.
- Produces: research metrics, factor attribution, frozen candidate identities, locked-test metrics, score invariance and identity audits.

- [x] Add a preregistration validator with exact dates, hashes, candidate levels, and no-promotion flags.
- [x] Add research-only loading through 2025-12-31 and evaluate all 25 candidates.
- [x] Write and hash `frozen_candidates.json` before any 2026 load.
- [x] Add locked 2026 loading and one evaluation pass over the frozen candidates.
- [x] Add active-control reproduction, trend-score invariance, no-lookahead phase ordering, and critical-file hash audits.
- [x] Run the experiment and inspect every generated CSV/JSON for finite values and expected row counts.

### Task 3: Archive, regression check, and conclusion

**Files:**
- Modify: `tests/test_identity_and_archives.py`
- Create: `experiments/0902_EX03/03_execution.md`
- Create: `experiments/0902_EX03/04_conclusion.md`
- Create: `experiments/0902_EX03/experiment_manifest.json`
- Modify: `docs/RESEARCH_HANDOFF.md`

**Interfaces:**
- Consumes: completed experiment artifacts and immutable audit results.
- Produces: one portable validated archive and cross-machine research handoff.

- [x] Update the archive-count regression to include `0902_EX03`.
- [x] Generate execution and conclusion documents from the recorded results.
- [x] Build and validate the experiment manifest.
- [x] Update the research handoff with the experiment status, exact rerun command, and evidence boundary.
- [x] Run `pytest tests/test_range_weight_research.py tests/test_identity_and_archives.py -q`.
- [x] Run `git diff --check` and inspect `git status --short`.
- [x] Commit the completed research archive on the feature branch without merging or pushing.
