# EX04 Return-only Challenger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Select and test a return-only challenger by optimizing only the fixed 12-factor weights and thresholds.

**Architecture:** Reuse EX03's exact four-layer factor and scoring primitives. Add a dedicated runner with expanding half-year fitting, return-only ranking, controlled weight/threshold attribution, freeze-before-holdout enforcement, and strict return-only PASS.

**Tech Stack:** Python 3.12, pandas, NumPy, CZSC 1.0.1, vectorbt 1.1.0, pytest.

**Spec:** `experiments/0824_EX04/02_design.md`

## Global Constraints

- Work on `codex/0824-ex04-return-recovery` without worktrees or subagents.
- Keep exactly the champion's 12 factors and all weights nonzero.
- Selection, ranking and PASS use only strategy return.
- Load 2026 only after writing the frozen challenger.
- Do not modify EX03, the baseline registry, normal backtests or factor definitions.

---

### Task 1: Return-only selection primitives

**Files:**
- Create: `src/czsc_trader/return_only_runner.py`
- Create: `tests/test_return_only_runner.py`

**Interfaces:**
- `build_return_specs(protocol) -> tuple[ReturnSpec, ...]`
- `return_objective(champion, challenger, weights, origin) -> tuple`
- `rank_return_results(rows) -> pd.DataFrame`
- `return_holdout_pass(windows) -> bool`

- [ ] Write failing tests for 144 configurations, return-only ordering, ignored Sharpe, strict three-window PASS and fixed nonzero factors.
- [ ] Implement the minimal pure primitives and run focused tests.

### Task 2: Formal runner

**Files:**
- Extend: `src/czsc_trader/return_only_runner.py`
- Extend: `tests/test_return_only_runner.py`

- [ ] Add tested half-year period construction and temporal training boundaries.
- [ ] Implement weight fitting, threshold validation, controlled attribution, freeze, holdout, audit, documents and manifest.
- [ ] Run focused factor, rule, backtest, audit and archive tests.

### Task 3: Execute EX04

- [ ] Commit preregistration and implementation before formal results.
- [ ] Execute once without post-holdout tuning.
- [ ] Verify archive boundaries, tests, compilation, dependencies, baseline hash and Git whitespace.
- [ ] Commit the immutable result without merging or pushing.

