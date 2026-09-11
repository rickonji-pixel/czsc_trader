# EX04 Mechanism Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute the preregistered `0825_EX02` diagnosis using only 2020—2025 data and archive reproducible mechanism, local-stability, path-concentration, regime, and bootstrap evidence.

**Architecture:** Add a focused attribution module containing deterministic pure statistics plus one runner that reuses the existing four-layer factor generation, scoring, state machine, period backtester, and archive builder. Extend the stable `scripts/run_experiment.py --experiment-dir ...` entrypoint to dispatch the new protocol type while retaining EX02 behavior.

**Tech Stack:** Python 3.12, pandas, NumPy, vectorbt, pytest, existing `czsc_trader` research primitives.

**Spec:** `experiments/0825_EX02/02_design.md`

## Global Constraints

- Work on `codex/0825-ex02-attribution`; do not use a worktree or subagent.
- Load no market filename containing `2026`; visible cutoff is exactly `2025-12-31`.
- Do not create a challenger, run a holdout, rank parameter candidates, or modify either baseline.
- The only normal research status is `COMPLETE`; failures archive `ERROR` and are re-raised.
- Use `scripts/run_experiment.py --experiment-dir experiments/0825_EX02` for formal execution.
- Formal execution starts from a clean committed implementation SHA.

---

### Task 1: Deterministic attribution primitives

**Files:**
- Create: `src/czsc_trader/ex04_attribution_runner.py`
- Create: `tests/test_ex04_attribution_runner.py`

**Interfaces:**
- Produces: `component_attribution(rows: pd.DataFrame) -> pd.DataFrame`
- Produces: `perturb_weight(weights: pd.Series, factor: str, delta: float) -> pd.Series`
- Produces: `classify_local_geometry(rows: pd.DataFrame, rules: Mapping[str, object]) -> dict[str, object]`
- Produces: `difference_intervals(baseline_target: pd.Series, ex04_target: pd.Series, baseline_returns: pd.Series, ex04_returns: pd.Series) -> pd.DataFrame`
- Produces: `market_regimes(close: pd.Series, lookback: int, up: float, down: float, lag: int) -> pd.Series`
- Produces: `paired_circular_block_bootstrap(baseline_returns: pd.Series, ex04_returns: pd.Series, *, block_length: int, replications: int, seed: int, quantiles: Sequence[float]) -> dict[str, object]`

- [ ] **Step 1: Write failing literal tests**

Test a hand-calculated 2x2 component game where baseline=0, weights-only=2, thresholds-only=3, combined=7; expected weight contribution is 3, threshold contribution is 4, and interaction is 2. Test proportional weight redistribution preserves positivity and sum one. Test geometry rule precedence, exact difference-interval boundaries, lagged 60-day regime labels, deterministic bootstrap output, and rejection of misaligned indices.

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_ex04_attribution_runner.py`

Expected: FAIL because `czsc_trader.ex04_attribution_runner` does not exist.

- [ ] **Step 3: Implement the minimal pure functions**

Implement only the declared APIs. All index alignment must be validated; bootstrap must use `numpy.random.default_rng(seed)` and circular block starts; interval contributions use `ex04_returns - baseline_returns` only on contiguous days where execution targets differ.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_ex04_attribution_runner.py`

Expected: PASS.

### Task 2: Pre-2026 experiment runner

**Files:**
- Modify: `src/czsc_trader/ex04_attribution_runner.py`
- Modify: `tests/test_ex04_attribution_runner.py`

**Interfaces:**
- Produces: `run_ex04_attribution(raw_dir: Path, baseline_root: Path, experiment_dir: Path, protocol: Mapping[str, object]) -> dict[str, object]`
- Consumes: `load_market_data(..., cutoff=pd.Timestamp('2025-12-31'))`, `generate_factor_frame`, `_equivalence`, `_PeriodEvaluator`, `score_four_layer`, `positions_from_scores`, `run_period_backtests`, and existing exact Shapley primitives.

- [ ] **Step 1: Write failing protocol and runner-boundary tests**

Test that protocol validation rejects holdout access, promotion flags, a wrong EX04 digest, non-32 perturbation configuration, and any visible hash containing `2026`. Test a controlled small-frame execution boundary writes the required artifact names and returns `status=COMPLETE`, `holdout_accessed=False`, and `frozen_challenger=None`.

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_ex04_attribution_runner.py`

Expected: FAIL because runner validation and orchestration are absent.

- [ ] **Step 3: Implement identity checks and all frozen diagnostics**

Load EX04 from the preregistered relative path, verify its raw SHA-256, validate factor names/order and baseline four-layer equivalence, then write:

```text
identity_audit.json
component_metrics.csv
component_attribution.csv
group_coalitions.csv
group_shapley.csv
group_interactions.csv
local_perturbations.csv
local_geometry.json
window_comparison.csv
difference_intervals.csv
regime_attribution.csv
bootstrap_summary.json
classification.csv
metrics.json
```

Compute annual and half-year results from the same frozen full-history targets. Compute the continuous-window equity-return paths with one independently funded 2021—2025 backtest per strategy. Do not write orders or holdout artifacts.

- [ ] **Step 4: Run focused regression**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_ex04_attribution_runner.py tests/test_four_layer_runner.py tests/test_attribution.py tests/test_backtest.py`

Expected: PASS.

### Task 3: Stable experiment entrypoint

**Files:**
- Modify: `scripts/run_experiment.py`
- Modify: `tests/test_experiment_entrypoints.py`

**Interfaces:**
- Produces: `run_preregistered_ex04_attribution(experiment_dir: Path) -> Path`
- CLI dispatch reads `artifacts/protocol.json` and routes `champion_attribution` to the existing EX02 path or `ex04_mechanism_attribution` to the new path.

- [ ] **Step 1: Write failing entrypoint tests**

Build a temporary `0825_EX02` archive, replace only the expensive runner boundary, and assert that the real entrypoint finalizes that same directory, records `COMPLETE`, preserves `holdout_accessed=false`, writes no frozen challenger, and rejects any promotion flag. Retain existing EX02 tests unchanged.

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_experiment_entrypoints.py`

Expected: FAIL because the CLI cannot dispatch the new protocol.

- [ ] **Step 3: Implement dispatch and deterministic documents**

On success write actual runtime/version metadata to `03_execution.md`, render conclusions from machine artifacts into `04_conclusion.md`, rebuild and validate the manifest. On exception archive `ERROR`, explicitly record no 2026 access, rebuild the manifest, and re-raise.

- [ ] **Step 4: Run entrypoint and focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_experiment_entrypoints.py tests/test_ex04_attribution_runner.py`

Expected: PASS.

### Task 4: Formal execution and research archive

**Files:**
- Modify: `experiments/0825_EX02/03_execution.md`
- Modify: `experiments/0825_EX02/04_conclusion.md`
- Create: machine artifacts declared by the spec
- Modify: `experiments/0825_EX02/experiment_manifest.json`
- Modify: `docs/RESEARCH_HANDOFF.md`

- [ ] **Step 1: Commit the implementation before data execution**

Run focused tests, `git diff --check`, verify the worktree contains only intended changes, and commit tests plus implementation. Record that clean commit as the execution SHA.

- [ ] **Step 2: Execute the formal experiment once**

Run:

```powershell
.\.venv\Scripts\python.exe scripts\run_experiment.py --experiment-dir experiments\0825_EX02
```

Expected: exit 0, status `COMPLETE`, no 2026 hashes, no `frozen_challenger.json`.

- [ ] **Step 3: Audit the machine evidence**

Verify all 32 perturbations are present, 8 group coalitions exist per window, 2000 bootstrap replications are reported, classifications match the protocol, and the path-dominance flag agrees with the interval CSV. Read the result files and replace any generic generated conclusion with an evidence-specific interpretation without changing machine classifications.

- [ ] **Step 4: Update handoff and rebuild the archive manifest**

Document `0825_EX02` as a diagnostic `COMPLETE` or truthful `ERROR`, distinguish it from PASS/FAIL challenges, list authoritative artifacts, and retain the two-baseline boundary.

- [ ] **Step 5: Final verification and result commit**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_ex04_attribution_runner.py tests/test_experiment_entrypoints.py tests/test_four_layer_runner.py tests/test_attribution.py tests/test_backtest.py
.\.venv\Scripts\python.exe -m compileall -q src tests scripts
.\.venv\Scripts\python.exe -c "from pathlib import Path; from czsc_trader.experiment_archive import validate_experiment_archive; [validate_experiment_archive(p) for p in sorted(Path('experiments').iterdir()) if p.is_dir()]; print('experiment archives: PASS')"
git diff --check
```

Commit the final research archive and handoff update. Do not merge or push unless the user requests it.
