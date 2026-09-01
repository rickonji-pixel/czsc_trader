# EX08 In-Memory Full Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run and archive a preregistered 4096-Trial EX08 study that changes only EX07's runtime storage and fixed Trial count, then freezes the winner and performs one 2026 holdout.

**Architecture:** Parameterize the existing EX07 orchestration instead of copying its backtest and holdout logic. Add an EX08 protocol validator and wrapper CLI that select `InMemoryStorage`, exact-count stopping, batch timing export, dynamic research documents, and the existing freeze-before-holdout gate.

**Tech Stack:** Python 3.12, Optuna 4.9.0, Joblib 1.5.3/loky, pandas, NumPy, vectorbt 1.1.0, pytest, Git-tracked experiment archives.

**Spec:** `docs/superpowers/specs/2026-08-24-ex08-inmemory-full-search-design.md`

## Global Constraints

- Experiment ID is `0824_EX08`; never overwrite `0824_EX07`.
- Selection data ends at `2025-12-31`; do not open `_2026.csv` before the frozen challenger is written and hashed.
- Freeze EX06's 91 candidates, EX07's 94-dimensional projection, TPE seed/config, maximin objective, ordered batches, and EX04 research baseline.
- Use pure `InMemoryStorage`, 8 loky workers, batch size 8, exactly 4096 completed Trials, no wall-time or stagnation stop.
- Trial 0 must replay EX04 with objective exactly 0.
- PASS requires strictly higher return than EX04 in all three 2026 windows; risk metrics are report-only.
- Commit goal, design, protocol, implementation, and tests before the formal run.
- Archive PASS, FAIL, or ERROR; runtime memory and engineering benchmarks are not research evidence.
- Work on `codex/0824-ex08-inmemory-search`; do not use worktrees or subagents.

---

### Task 1: Preregister EX08

**Files:**
- Create: `experiments/0824_EX08/01_goal.md`
- Create: `experiments/0824_EX08/02_design.md`
- Create: `experiments/0824_EX08/implementation_plan.md`
- Create: `experiments/0824_EX08/artifacts/protocol.json`

**Interfaces:**
- Produces: immutable formal protocol consumed by the EX08 validator and runner.

- [ ] Write the goal with EX04 baseline, return-only PASS, pre-2026 boundary, and fixed 4096 completion rule.
- [ ] Write the design with the exact EX07 candidate hashes, TPE parameters, memory storage, failure semantics, freeze gate, and artifacts.
- [ ] Copy this implementation plan into the experiment directory.
- [ ] Write protocol JSON using `minimum_completed_trials=4096`, `maximum_completed_trials=4096`, `no_improvement_trials=null`, `maximum_wall_time_seconds=null`, and `storage_mode="memory"`.
- [ ] Run `git diff --check`, then commit the preregistration separately from runtime results.

### Task 2: Support exact-count searches without optional early stops

**Files:**
- Modify: `src/czsc_trader/optuna_search.py`
- Modify: `tests/test_optuna_search.py`

**Interfaces:**
- Changes `run_study_batches(..., no_improvement_trials: int | None, maximum_wall_time_seconds: float | None, ...)`.
- Existing integer EX07 behavior remains unchanged.

- [ ] Write a failing synthetic test passing both optional stops as `None` and requiring exactly 16 completed Trials.
- [ ] Run the focused test and confirm RED from comparing `None` numerically.
- [ ] Guard validation and stop checks with `is not None` while preserving maximum completed Trials as mandatory.
- [ ] Run all `test_optuna_search.py` tests and confirm GREEN.
- [ ] Commit the exact-count stopping support.

### Task 3: Add EX08 protocol and selection validation

**Files:**
- Modify: `src/czsc_trader/optuna_runner.py`
- Create: `tests/test_ex08_runner.py`

**Interfaces:**
- Produces `validate_ex08_protocol(protocol: Mapping[str, object]) -> None`.
- Extends `prepare_selection_inputs(..., protocol_validator: Callable = validate_ex07_protocol)`.

- [ ] Write failing tests that accept the committed EX08 protocol and reject SQLite, non-4096 limits, a wall limit, stagnation, changed TPE settings, changed candidates, or holdout windows.
- [ ] Write a failing data-loader test proving the EX08 selection context does not open 2026 partitions.
- [ ] Implement the EX08 validator with literal expected mappings and pass it into the generalized selection loader.
- [ ] Run EX08 and existing EX07 runner tests; confirm both protocols remain isolated.
- [ ] Commit protocol and boundary validation.

### Task 4: Parameterize formal orchestration for memory execution

**Files:**
- Modify: `src/czsc_trader/optuna_runner.py`
- Modify: `tests/test_ex08_runner.py`
- Modify: `tests/test_optuna_runner.py`

**Interfaces:**
- Extends `run_optuna_experiment` with explicit validator, storage mode, timing collection, and recovery controls while keeping EX07 defaults unchanged.
- Extends the existing `python -m czsc_trader.optuna_runner` CLI with explicit storage, full-count, and execution-commit arguments.

- [ ] Write failing orchestration tests showing EX08 requests memory storage, skips runtime recovery, collects 512 timing batches, refuses to freeze fewer than 4096 completed Trials, and never loads holdout before a frozen hash exists.
- [ ] Parameterize the formal runner and export `batch_timings.csv` when timing collection is enabled.
- [ ] Make research document labels derive from `experiment_id`, so EX08 docs never say EX07.
- [ ] After holdout, rewrite `study_summary.json` with `holdout_accessed=true`, frozen SHA-256, storage mode, execution commit, and timing summary.
- [ ] Route EX08 through the existing Optuna CLI by its preregistered experiment ID; do not add another script entrypoint.
- [ ] Run focused EX07/EX08 tests and commit the formal runner.

### Task 5: Verify and commit the executable preregistration

**Files:**
- All implementation and preregistration files from Tasks 1-4.

- [ ] Run focused tests for search, EX07 runner, EX08 runner, archives, and experiment entrypoints.
- [ ] Run `python -m compileall -q src scripts tests`, `python -m pip check`, and `git diff --check`.
- [ ] Confirm `git diff --name-only` contains no EX07 artifact change.
- [ ] Commit any remaining tracked implementation files.
- [ ] Record the clean execution commit SHA in the formal command log before starting the search.

### Task 6: Run the formal 4096-Trial study

**Files:**
- Generate: `experiments/0824_EX08/03_execution.md`
- Generate: `experiments/0824_EX08/04_conclusion.md`
- Generate: `experiments/0824_EX08/experiment_manifest.json`
- Generate: all protocol-required files under `experiments/0824_EX08/artifacts/`

- [ ] Run `./.venv/Scripts/python.exe -m czsc_trader.optuna_runner --experiment-dir experiments/0824_EX08 --storage-mode memory --require-full-trial-count --execution-commit <SHA>` once from a clean committed tree.
- [ ] Monitor without modifying protocol or parameters; if it fails before holdout, record the failed attempt and restart only from Trial 0 under the same commit.
- [ ] Verify 4096 COMPLETE, zero failed Trials, 512 timing rows, Trial 0 objective 0, and exact candidate identity.
- [ ] Verify the winner was re-evaluated uncached and `frozen_challenger.json` existed with SHA-256 before holdout loading.
- [ ] Read the actual returned experiment directory and archive PASS or FAIL without reinterpretation.

### Task 7: Final verification and result commit

**Files:**
- Track the completed `experiments/0824_EX08/` archive.
- Update: `docs/RESEARCH_HANDOFF.md` only after reading final results.

- [ ] Update the handoff with EX08's exact Trial count, winner, result, storage mode, and new research baseline status without changing the general `baseline_20260823` identity.
- [ ] Run the focused suite and full `pytest -q`.
- [ ] Run compileall, pip check, diff check, and validate every experiment archive.
- [ ] Confirm no runtime SQLite, ignored output, or untracked file enters the commit.
- [ ] Commit the complete PASS, FAIL, or ERROR archive and handoff.
- [ ] Report the branch, commits, actual output, three holdout windows, audit status, performance, tests, and whether the branch has been pushed.
