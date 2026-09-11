# EX07 Optuna Joint Strategy Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and execute a resumable Optuna search that jointly selects the EX06 factor subset, signed weights, and entry/exit thresholds, then freezes and evaluates one EX07 challenger.

**Architecture:** Keep EX06 factor generation and the existing backtest/state machine unchanged. Add a pure projection module and an EX07 runner that owns Optuna ask/tell, delegates read-only trial evaluation to Joblib workers, persists runtime state in ignored SQLite, and exports formal text artifacts.

**Tech Stack:** Python 3.12, Optuna 4.9.0, Joblib 1.5.3 with loky, pandas, NumPy, vectorbt 1.1.0, pytest.

**Spec:** `experiments/0824_EX07/02_design.md`

## Global Constraints

- Develop on `codex/0824-ex07-optuna`; do not use a Git worktree or subagent.
- Do not open any 2026 K-line before `frozen_challenger.json` is written and hashed.
- Candidate names and order must exactly equal the 91 rows frozen by EX06.
- Selection uses return only; Sharpe never influences Optuna values, ranking, or PASS.
- Runtime SQLite remains ignored; final evidence is exported to tracked CSV/JSON/Markdown.
- Use TDD and focused tests for each production behavior.

---

### Task 1: Add Optuna and deterministic sparse projection

**Files:**
- Modify: `pyproject.toml`
- Create: `src/czsc_trader/optuna_search.py`
- Create: `tests/test_optuna_search.py`

**Interfaces:**
- Consumes: EX06 factor names, EX04 origin weights, and EX07 protocol mappings.
- Produces: `ProjectedStrategy`, `project_trial_parameters`, `suggest_trial_parameters`, `trial_sort_key`, and `validate_optuna_protocol`.

- [ ] **Step 1: Write the failing projection test**

```python
def test_projection_is_sparse_normalized_and_protected():
    strategy = project_trial_parameters(names, raw, 6, 0.18, 0.12, origin, protocol)
    assert strategy.weights.ne(0).sum() == 6
    assert strategy.weights.abs().sum() == pytest.approx(1.0)
    assert strategy.weights[strategy.weights.ne(0)].abs().min() >= 0.0125
    assert strategy.weights.filter(like="vol_window_V230731").abs().sum() > 0
    assert strategy.weights.loc[trend_names].abs().sum() >= 0.10
    assert strategy.enter == pytest.approx(0.18)
    assert strategy.exit == pytest.approx(0.06)
```

Also test stable ties, zero-sign fallback, bounds rejection, exact protocol fields, and formal ranking order.

- [ ] **Step 2: Verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_optuna_search.py -q`

Expected: collection fails because `czsc_trader.optuna_search` does not exist.

- [ ] **Step 3: Implement the minimum production API**

Add `"optuna==4.9.0"` to dependencies. Implement `ProjectedStrategy` and `project_trial_parameters`. Select by `(-abs(raw), factor_name)`, inject protected volume/trend identities, allocate the 0.0125 floor before proportional residual weight, transfer slack until trend mass is 0.10, restore signs, and call `validate_sparse_weights`.

- [ ] **Step 4: Verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_optuna_search.py tests/test_factor_discovery.py -q`

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml src/czsc_trader/optuna_search.py tests/test_optuna_search.py
git commit -m "research: add Optuna strategy projection"
```

### Task 2: Add deterministic batched ask/evaluate/tell and resume

**Files:**
- Modify: `src/czsc_trader/optuna_search.py`
- Modify: `tests/test_optuna_search.py`

**Interfaces:**
- Consumes: an Optuna study, a Trial-to-strategy callable, and a batch evaluator.
- Produces: `TrialRequest`, `TrialOutcome`, `run_study_batches`, `recover_running_trials`, and `export_study_trials`.

- [ ] **Step 1: Write failing orchestration tests**

Use temporary SQLite and a synthetic objective. Assert ascending ask order, sorted tell order despite reversed worker results, identical one-worker/two-worker parameters and ranking, stale RUNNING recovery to FAIL, and no duplicate Trial 0 on resume.

- [ ] **Step 2: Verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_optuna_search.py -q`

- [ ] **Step 3: Implement parent-only orchestration**

Create the fixed TPE study, store the protocol hash in study attributes, enqueue EX04 only for an empty study, use explicit `ask`/`tell`, and enforce the three preregistered stop conditions. Missing, duplicate, or unknown trial results raise `RuntimeError`; strategy exceptions create FAIL trials. Export flat UTF-8 CSV with parameters, values, user attributes, timing and state.

- [ ] **Step 4: Verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_optuna_search.py -q`

- [ ] **Step 5: Commit**

```powershell
git add src/czsc_trader/optuna_search.py tests/test_optuna_search.py
git commit -m "research: add resumable batched Optuna search"
```

### Task 3: Add the causal EX07 experiment runner

**Files:**
- Create: `src/czsc_trader/optuna_runner.py`
- Create: `tests/test_optuna_runner.py`
- Modify: `tests/test_experiment_entrypoints.py`

**Interfaces:**
- Consumes: EX07 protocol, EX04 baseline, EX06 candidate artifacts, validated data, factor generator, batch engine, backtest and audit utilities.
- Produces: `validate_ex07_protocol`, `build_ex07_candidate_matrix`, `evaluate_trial_batch`, `run_optuna_experiment`, and module CLI.

- [ ] **Step 1: Write failing boundary tests**

Write six explicit tests: compare the generated name tuple and source hashes with EX06; record every loader filename and assert none contains `_2026.csv` during selection; change only synthetic Sharpe values and assert the objective is unchanged; snapshot the artifacts directory before and after a worker call and assert equality; record calls to frozen-file write and unrestricted load and assert write occurs first; build an experiment manifest beside a runtime database and assert no runtime path appears in `files`.

- [ ] **Step 2: Verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_optuna_runner.py tests/test_experiment_entrypoints.py -q`

- [ ] **Step 3: Implement selection**

Load only through cutoff 2025-12-31, validate preregistered hashes, generate and compare all 91 factor names, compute EX04 metrics once, and evaluate projected strategies over eight windows. Workers receive immutable arrays and return data only. Joblib uses loky, eight processes, memmapping and one inner thread. Parent deduplicates target digests and restores completed metrics from Optuna user attributes on resume.

- [ ] **Step 4: Implement freeze and holdout**

Rank COMPLETE trials by the fixed tuple, rebuild and uncached-verify the winner, write weights and frozen JSON, hash it, then load 2026. Report EX04, EX06, legacy champion, Buy & Hold and EX07; run causal audit and write holdout metrics plus orders.

- [ ] **Step 5: Verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_optuna_search.py tests/test_optuna_runner.py tests/test_factor_discovery.py tests/test_factor_discovery_runner.py tests/test_four_layer_runner.py tests/test_experiment_entrypoints.py -q`

- [ ] **Step 6: Commit**

```powershell
git add src/czsc_trader/optuna_runner.py tests/test_optuna_runner.py tests/test_experiment_entrypoints.py
git commit -m "research: implement EX07 Optuna runner"
```

### Task 4: Preregister and execute EX07

**Files:**
- Modify: `.gitignore`
- Create: `experiments/0824_EX07/01_goal.md`
- Create: `experiments/0824_EX07/02_design.md`
- Create: `experiments/0824_EX07/implementation_plan.md`
- Create: `experiments/0824_EX07/artifacts/protocol.json`
- Runtime only: `experiments/0824_EX07/runtime/optuna.db`

**Interfaces:**
- Consumes: completed EX07 runner and frozen protocol.
- Produces: one formal resumable study and all tracked selection/holdout evidence.

- [ ] **Step 1: Validate and commit preregistration**

Parse JSON, validate source hashes and run `git diff --check`. Commit only `.gitignore`, goal, design, plan and protocol with message `research: preregister EX07 Optuna search`.

- [ ] **Step 2: Install and verify**

Run `.venv\Scripts\python.exe -m pip install -e .`, then focused EX07 tests. Require Optuna 4.9.0.

- [ ] **Step 3: Execute the formal study**

Run `.venv\Scripts\python.exe -m czsc_trader.optuna_runner --experiment-dir experiments/0824_EX07 --raw-dir data/raw --baseline-dir baselines`. If interrupted, rerun the identical command and require study metadata to match.

- [ ] **Step 4: Verify formal artifacts**

Require candidate identity, all trials CSV, study summary, weights, frozen challenger, holdout metrics, orders and causal audit. Independently reconstruct the winner and confirm the runtime database is ignored.

- [ ] **Step 5: Finish the archive**

Write `03_execution.md` and `04_conclusion.md`, build `experiment_manifest.json` last, validate the archive, run all focused tests plus `git diff --check`, and commit source, tests, dependency metadata and formal EX07 artifacts. Do not commit runtime SQLite files.
