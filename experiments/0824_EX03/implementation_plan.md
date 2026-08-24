# Fixed-factor Four-layer Challenger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and evaluate a four-layer challenger while preserving all 12 champion signals.

**Architecture:** Flatten the champion group averages into an exactly equivalent 12-signal linear model. Optimize only nonzero signed weights and thresholds with deterministic expanding-window coordinate search, freeze one model, then open the 2026 holdout once.

**Tech Stack:** Python 3.12, pandas, NumPy, CZSC 1.0.1, vectorbt 1.1.0, pytest.

**Spec:** `experiments/0824_EX03/02_design.md`

## Global Constraints

- Work on `codex/0824-ex03-fixed-factor-four-layer` without worktrees or subagents.
- Keep exactly the champion's 12 signal identities and normalization rules.
- Prove score error at most `1e-12` and exact position equality before optimization.
- Keep all 12 optimized weights nonzero with minimum absolute weight `0.005`.
- Do not alter the champion state machine, fees, execution model, baseline registry, previous experiments, or normal backtest entrypoint.
- Load 2026 only after writing the frozen challenger.

---

### Task 1: Exact four-layer representation

**Files:**
- Create: `src/czsc_trader/four_layer.py`
- Create: `tests/test_four_layer.py`

**Interfaces:**
- `normalized_signal_factors(raw) -> pd.DataFrame`
- `flatten_champion_weights(columns, groups, group_weights) -> pd.Series`
- `score_four_layer(factors, weights) -> pd.Series`
- `positions_from_scores(scores, enter, exit, state_rule) -> pd.Series`

- [ ] Write tests asserting 12 exact identities, expected per-group weights, score equality, position equality, and nonzero-weight validation.
- [ ] Run the test and confirm missing-module failure.
- [ ] Implement the minimum pure representation and state-machine adapter.
- [ ] Run four-layer and existing rule tests.

### Task 2: Fixed-factor optimizer and runner

**Files:**
- Create: `src/czsc_trader/four_layer_runner.py`
- Create: `tests/test_four_layer_runner.py`

**Interfaces:**
- `build_optimizer_specs(protocol) -> tuple[OptimizerSpec, ...]`
- `coordinate_optimize(...) -> pd.Series`
- `run_four_layer_experiment(raw_dir, baseline_root, experiment_dir) -> dict`

- [ ] Write failing tests for 72 configurations, deterministic ranking, no dropped factors, frozen-before-holdout ordering, and strict PASS.
- [ ] Implement expanding-window fitting, direct portfolio objective, freeze, holdout, causal audit, artifacts, narrative and manifest.
- [ ] Run focused runner, factor, backtest, audit and archive tests.

### Task 3: Formal execution

**Files:**
- Populate: `experiments/0824_EX03/artifacts/`
- Finalize: `experiments/0824_EX03/03_execution.md`
- Finalize: `experiments/0824_EX03/04_conclusion.md`
- Create: `experiments/0824_EX03/experiment_manifest.json`

- [ ] Commit preregistration and implementation before result generation.
- [ ] Execute EX03 once, without post-holdout tuning.
- [ ] Verify focused tests, compileall, pip check, manifest, baseline hash and Git whitespace.
- [ ] Commit the immutable result and report it without merging or pushing.
