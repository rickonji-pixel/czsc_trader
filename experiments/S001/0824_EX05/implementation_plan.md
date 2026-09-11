# EX05 State-Expanded Factor Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and execute a preregistered EX05 challenger that may add, remove, and replace CZSC factors while selecting only on pre-2026 returns and comparing once against EX04 on 2026.

**Architecture:** A focused factor-discovery module generates the exact candidate universe and performs constrained sparse coordinate search. A separate runner owns protocol validation, rolling selection, freezing, holdout access, three-baseline reporting, causal audit, documents, and manifest generation.

**Tech Stack:** Python 3.12, pandas, NumPy, CZSC 1.0.1, vectorbt 1.1.0, pytest.

**Spec:** `experiments/0824_EX05/02_design.md`

## Global Constraints

- No 2026 file may be opened before `frozen_challenger.json` is written and hashed.
- Selection and PASS use strategy return only; Sharpe is report-only.
- The final score is one linear sum of at most 18 nonzero factor weights followed by the frozen long/cash state machine.
- Existing EX04, legacy baseline files, ordinary backtest behavior, and earlier experiment archives are immutable.
- Use TDD for production behavior and run the complete suite before delivery.

---

### Task 1: Candidate-universe generation

**Files:**
- Create: `src/czsc_trader/factor_discovery.py`
- Create: `tests/test_factor_discovery.py`

**Interfaces:**
- Produces: `generate_candidate_factors(data, protocol, frozen_names=None) -> CandidateFactors`
- Produces: `validate_sparse_weights(weights, factor_names, protocol) -> None`

- [ ] Write failing tests proving deterministic state names, coverage/frequency filtering, exact deduplication, four interaction columns, and frozen-name replay with unseen states mapped to zero.
- [ ] Run `pytest tests/test_factor_discovery.py -v` and confirm import/behavior failures.
- [ ] Implement `CandidateFactors`, exact new-signal configs, state expansion, deterministic deduplication, interactions, metadata, and sparse-weight validation.
- [ ] Run `pytest tests/test_factor_discovery.py -v` and confirm all tests pass.

### Task 2: Sparse optimizer and return-only ranking

**Files:**
- Modify: `src/czsc_trader/factor_discovery.py`
- Modify: `tests/test_factor_discovery.py`

**Interfaces:**
- Produces: `sparse_coordinate_optimize(origin, step, rounds, protocol, evaluator) -> pd.Series`
- Produces: `rank_factor_results(rows) -> pd.DataFrame`

- [ ] Write failing tests proving zero-weight activation, existing-factor removal, L1 normalization, active-factor cap, trend/volume protection, deterministic tie handling, and ranking independence from Sharpe.
- [ ] Run the focused tests and confirm failure.
- [ ] Implement the minimal greedy optimizer and ranking logic with no hidden randomness.
- [ ] Run `pytest tests/test_factor_discovery.py -v` and confirm all tests pass.

### Task 3: Experiment orchestration and boundary enforcement

**Files:**
- Create: `src/czsc_trader/factor_discovery_runner.py`
- Create: `tests/test_factor_discovery_runner.py`

**Interfaces:**
- Produces: `run_factor_discovery_experiment(raw_dir, baseline_root, ex04_path, experiment_dir, fee_rate=0.0005, init_cash=1_000_000) -> dict`
- Produces: CLI `python -m czsc_trader.factor_discovery_runner --experiment-dir experiments/0824_EX05`

- [ ] Write failing tests for protocol validation, 64 configurations, training windows strictly before validation, EX04-only PASS, frozen-before-holdout sequencing, and three-baseline output schema.
- [ ] Run `pytest tests/test_factor_discovery_runner.py -v` and confirm failure.
- [ ] Implement selection-data loading, EX04 replay, rolling sparse fitting, candidate CSVs, final refit, freeze/hash, holdout replay, legacy and Buy & Hold metrics, audit, docs, and manifest.
- [ ] Run both EX05 test modules plus related backtest/factor/archive tests.

### Task 4: Preregister, execute, and preserve evidence

**Files:**
- Modify: `experiments/0824_EX05/03_execution.md`
- Modify: `experiments/0824_EX05/04_conclusion.md`
- Create: generated files under `experiments/0824_EX05/artifacts/`
- Create: `experiments/0824_EX05/experiment_manifest.json`

- [ ] Commit the protocol, implementation, and green focused tests before the formal run.
- [ ] Run the EX05 module once; do not change protocol or code after seeing 2026 results except to correct a demonstrated implementation defect, which requires rerunning under a new experiment ID.
- [ ] Verify candidate hashes omit 2026, factor count and protection constraints, freeze order, PASS logic, three baselines, and causal audit.
- [ ] Run `pytest`, `compileall`, `pip check`, manifest validation, and `git diff --check`.
- [ ] Commit all PASS or FAIL evidence and report without promoting the legacy baseline.
