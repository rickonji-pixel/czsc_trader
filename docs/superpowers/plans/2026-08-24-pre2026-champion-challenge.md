# Pre-2026 Champion Challenge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run a causally isolated 588080 champion-challenger experiment using only listing-to-2025 data, and expose 2026 only to a separately frozen holdout evaluator.

**Architecture:** Extend the manifest loader with a cutoff that skips future annual files before opening them. Add a narrow re-entry experiment with 20 predeclared cooldown/gate challengers around the immutable champion, evaluate 2023-2025 annual return and Sharpe, freeze at most one challenger, and keep 2026 evaluation in a separate entrypoint.

**Tech Stack:** Python 3.12, pandas, CZSC 1.0.1, vectorbt 1.1.0, pytest, JSON.

**Spec:** User-approved small-team research protocol in the current conversation.

## Global Constraints

- Research code must not open any 2026 K-line file or use any 2026 factor, metric, chart, or output.
- Champion is immutable `baseline_20260823`.
- A window passes only when challenger return is strictly greater than champion return and challenger Sharpe is strictly greater than champion Sharpe.
- Drawdown, costs, turnover, exposure, and trade count are observational only and cannot affect PASS or ranking.
- Research windows are calendar years 2023, 2024, and 2025; all three must pass before a challenger is frozen.
- The first hypothesis changes only post-exit re-entry cooldown and re-entry gate.
- No Git worktree and no automatic baseline promotion.

---

### Task 1: Enforce pre-2026 data isolation

**Files:**
- Modify: `src/czsc_trader/data.py`
- Modify: `tests/test_data.py`

- [x] Add a failing test proving `load_market_data(..., cutoff="2025-12-31")` never opens a 2026 CSV.
- [x] Filter manifest records by year before reading or hashing files; trim bars to the cutoff and reconcile only the visible subset.
- [x] Run `pytest tests/test_data.py -q` and verify GREEN.

### Task 2: Implement the narrow champion-challenger experiment

**Files:**
- Create: `src/czsc_trader/experiments.py`
- Create: `tests/test_experiments.py`
- Create: `configs/experiments/588080_reentry_v1.json`

- [x] Add failing tests for cooldown semantics, strict return-and-Sharpe PASS, all-window PASS, and deterministic ranking.
- [x] Implement 20 predeclared challengers from cooldown `[0,2,3,5,8]` and gate `[none,structure,trend,structure_and_trend]` while preserving all champion parameters.
- [x] Evaluate champion and challengers on 2023, 2024, and 2025 with identical costs and independent portfolios.
- [x] Rank only by pass count, worst/mean return delta, worst/mean Sharpe delta, then complexity and stable ID.
- [x] Freeze a challenger only if every annual window passes.

### Task 3: Add separate research and holdout entrypoints

**Files:**
- Create: `scripts/run_experiment.py`
- Create: `scripts/run_holdout.py`
- Modify: `README.md`
- Modify: `docs/RESEARCH_HANDOFF.md`

- [x] Make `run_experiment.py` hard-code or validate `sample_end <= 2025-12-31` and export protocol, candidates, selected challenger, metrics, hashes, and audit evidence.
- [x] Make `run_holdout.py` require a frozen challenger JSON and compare it with the champion on 2026Q1, 2026H1, and 2026M1-M8.
- [x] Ensure holdout PASS uses only strict return and Sharpe comparisons in all three windows.
- [x] Document the two-stage workflow and the prohibition on opening 2026 before challenger freeze.

### Task 4: Restore listing-to-2026 market history and run the experiment

**Files:**
- Modify: `data/raw/588080_*`
- Generate locally: `outputs/<actual experiment directory>`

- [x] Fetch 588080 from `2020-01-01` through `2026-08-21` using the existing Tushare HFQ pipeline.
- [x] Verify manifest, adjustment factor, 30m/daily reconciliation, daily/weekly reconciliation, and file hashes.
- [x] Run the pre-2026 experiment and report PASS or FAIL without accessing holdout during selection.
- [x] If and only if a challenger passes, commit its frozen JSON before running the separate 2026 holdout. (No challenger passed; no frozen file or holdout run was produced.)

### Task 5: Verify and deliver

- [x] Run the focused experiment tests.
- [x] Run the full fast offline suite.
- [x] Run compileall, pip check, snapshot consistency, and `git diff --check`.
- [x] Confirm the champion rule file is unchanged and report the exact research verdict.
