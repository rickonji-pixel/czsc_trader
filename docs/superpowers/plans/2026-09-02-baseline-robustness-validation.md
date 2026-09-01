# Baseline Robustness Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, execute, and archive `0902_EX01`, a non-optimizing robustness validation of active baseline `baseline_20260901`.

**Architecture:** Add a focused pure-statistics module for CSCV/PBO, Deflated Sharpe, parameter geometry, and cyclic-shift ranking. Keep experiment-specific reconstruction and archive writing in a tracked runner inside the experiment directory so the production CLI remains lean; use existing baseline/factor/backtest APIs and emit an immutable standard archive.

**Tech Stack:** Python 3.12, pandas, NumPy, SciPy already present through the environment, Plotly already present through the project, pytest, existing czsc_trader backtest/archive APIs.

**Spec:** `docs/superpowers/specs/2026-09-02-baseline-robustness-validation-design.md`

## Global Constraints

- Work on `codex/0902-baseline-robustness-audit`; do not create a worktree or use a subagent.
- Do not modify, optimize, reselect, promote, or demote `baseline_20260901`.
- Do not update market data; freeze the tracked test end at `2026-09-01`.
- Use 2020 only for warmup, 2021—2025 for grid reconstruction and search diagnostics, and frozen 2026 only for placebo alignment.
- Reconstruct exactly 625 EX20 candidates and abort if candidate143 does not reproduce the EX20 archive.
- Report PBO, DSR, parameter geometry, cyclic-shift evidence, and the project trial ledger separately; add no composite PASS gate.
- Keep production CLI resources unchanged and retain only the existing `tests/test_cli_e2e.py` test file.
- Use `apply_patch` for tracked edits and run only focused tests plus archive validation.

---

### Task 1: Pure robustness statistics

**Files:**
- Create: `src/czsc_trader/robustness.py`
- Modify: `tests/test_cli_e2e.py`

**Interfaces:**
- Produces: `annualized_sharpe(returns: np.ndarray) -> float`
- Produces: `contiguous_blocks(length: int, block_count: int) -> tuple[np.ndarray, ...]`
- Produces: `cscv_pbo(returns: pd.DataFrame, block_count: int = 10) -> tuple[pd.DataFrame, dict[str, float | int]]`
- Produces: `deflated_sharpe_ratio(selected_returns: pd.Series, trial_sharpes: pd.Series) -> dict[str, float | int]`
- Produces: `parameter_geometry(candidates: pd.DataFrame, selected_id: int, parameter_columns: tuple[str, ...]) -> tuple[pd.DataFrame, pd.DataFrame]`
- Produces: `cyclic_shifts(values: pd.Series) -> tuple[pd.Series, ...]`

- [ ] **Step 1: Add failing synthetic CSCV and cyclic-shift tests**

Add tests that assert 10 blocks cover each row exactly once, 252 complementary CSCV rows are emitted, an intentionally overfit training winner yields the expected validation-rank direction, and a length-five series yields four unique non-identity shifts with preserved value counts.

- [ ] **Step 2: Run focused tests and verify missing imports fail**

Run: `\.\.venv\Scripts\python.exe -m pytest -q tests/test_cli_e2e.py -k "cscv or cyclic"`

Expected: FAIL because `czsc_trader.robustness` does not exist.

- [ ] **Step 3: Implement deterministic block, Sharpe, CSCV, and shift primitives**

Use `np.array_split` for chronological blocks, `itertools.combinations(range(10), 5)` for all 252 training halves, stable candidate-ID tie breaking, descending validation ranks, clipped rank fractions for finite logits, and `Series.shift(-lag)` plus wrapped concatenation for lags `1..N-1`.

- [ ] **Step 4: Add failing DSR and parameter-geometry tests**

Use fixed synthetic returns to assert every reported DSR input is finite, higher selected Sharpe increases DSR probability, Manhattan distance one identifies exact grid neighbors, and candidate identity/order is stable.

- [ ] **Step 5: Implement DSR and geometry**

Implement the Bailey-Lopez de Prado expected maximum Sharpe approximation using 625 trial sharpes, daily sample skew and Pearson kurtosis, and `scipy.stats.norm.cdf`. Validate at least two observations, two finite trials, positive variance, and positive denominator. Geometry normalizes each coordinate by its grid step and outputs distances, selected-relative metric changes, and immediate/two-step neighbor tables.

- [ ] **Step 6: Run focused tests**

Run: `\.\.venv\Scripts\python.exe -m pytest -q tests/test_cli_e2e.py -k "robustness or cscv or cyclic or deflated or geometry"`

Expected: PASS.

- [ ] **Step 7: Commit pure statistics**

```powershell
git add src/czsc_trader/robustness.py tests/test_cli_e2e.py
git commit -m "feat: add strategy robustness statistics"
```

---

### Task 2: Preregister the experiment and build the reproducible runner

**Files:**
- Create: `experiments/0902_EX01/01_goal.md`
- Create: `experiments/0902_EX01/02_design.md`
- Create: `experiments/0902_EX01/artifacts/protocol.json`
- Create: `experiments/0902_EX01/run_experiment.py`

**Interfaces:**
- Consumes: active baseline registry, EX20 protocol/frozen challenger/candidate results, existing factor and backtest APIs, Task 1 statistics.
- Produces: `reconstruct_candidate_paths(repo_root: Path) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]`
- Produces: `run_placebo_audit(...) -> tuple[pd.DataFrame, dict[str, object]]`
- Produces: `build_trial_ledger(experiments_root: Path, before_id: str) -> tuple[pd.DataFrame, dict[str, object]]`
- Produces: `main() -> int`

- [ ] **Step 1: Write goal, design, and machine-readable protocol**

Copy the exact frozen identities, dates, 625 grid, 10 CSCV blocks, 252 splits, Sharpe primary statistic, all legal nonzero 2026 shifts, empirical p-value formula, artifact list, and no-composite-gate rule from the design spec.

- [ ] **Step 2: Implement identity and source-data preflight**

Resolve `baseline_20260901`; assert candidate143 and strategy `czsc_regime_weight`; compare its frozen rule with EX20’s `frozen_challenger.json`; hash the registry, rule, EX20 protocol/challenger, market manifest/validation files, and relevant source modules into `identity_audit.json`.

- [ ] **Step 3: Implement exact 625-path reconstruction**

Load market data only through 2025 first, generate the shared normalized factor frame and lagged ER labels, enumerate the EX20 multiplier order, run the frozen state machine and backtest for each candidate, and retain aligned daily equity returns and all comparison metrics. Compare candidate143 and candidate IDs against EX20 artifacts before continuing.

- [ ] **Step 4: Implement PBO, DSR, and parameter artifacts**

Write `candidate_daily_returns.npz`, `candidate_return_index.csv`, `candidate_metrics.csv`, `cscv_splits.csv`, `pbo_summary.json`, `deflated_sharpe.json`, `parameter_surface.csv`, and `neighbor_geometry.csv`. Create one self-contained Plotly HTML with the six candidate143-anchored two-parameter slices.

- [ ] **Step 5: Implement exact 2026 placebo enumeration**

Only after the 2021—2025 reproduction gate passes, load through `2026-09-01`; generate the frozen target sequence; evaluate the unshifted strategy and every nonzero circular shift with identical fee/execution accounting. Write `placebo_shifts.csv` and `placebo_summary.json` with the exact empirical p-value and percentile.

- [ ] **Step 6: Implement the historical trial ledger**

Scan the 46 preceding manifests/protocols and extract only evidenced fields. Parse `candidate_count`, Optuna trial counts, candidate-result row counts, and documented counts with an explicit `count_basis`; leave unavailable values null. Write `trial_ledger.csv` and `trial_ledger_summary.json` without combining counts into DSR/PBO.

- [ ] **Step 7: Write the archive narrative and manifest**

Write `03_execution.md`, `04_conclusion.md`, `artifacts/metrics.json`, reconstruction audit, post-run identity audit, and `experiment_manifest.json`. Conclusions must separately state search bias, DSR evidence, parameter shape, placebo alignment, and ledger completeness.

- [ ] **Step 8: Commit preregistration and runner before observing formal outputs**

```powershell
git add experiments/0902_EX01/01_goal.md experiments/0902_EX01/02_design.md experiments/0902_EX01/artifacts/protocol.json experiments/0902_EX01/run_experiment.py
git commit -m "research: preregister baseline robustness validation"
```

---

### Task 3: Execute, audit, and archive 0902_EX01

**Files:**
- Modify: `experiments/0902_EX01/03_execution.md`
- Modify: `experiments/0902_EX01/04_conclusion.md`
- Create: all files declared in `experiments/0902_EX01/artifacts/protocol.json`
- Create: `experiments/0902_EX01/experiment_manifest.json`
- Modify: `tests/test_cli_e2e.py`

**Interfaces:**
- Consumes: Tasks 1—2.
- Produces: complete immutable Git-tracked research archive and project-level archive validation count 47.

- [ ] **Step 1: Run the focused statistics tests**

Run: `\.\.venv\Scripts\python.exe -m pytest -q tests/test_cli_e2e.py -k "robustness or cscv or cyclic or deflated or geometry"`

Expected: PASS.

- [ ] **Step 2: Execute the preregistered experiment exactly once**

Run: `\.\.venv\Scripts\python.exe experiments/0902_EX01/run_experiment.py`

Expected: exit code 0 with a complete archive, or a preserved ERROR archive if an immutable reproduction gate fails.

- [ ] **Step 3: Inspect evidence and finalize the two narrative documents**

Populate execution with actual timings/counts/checksums and conclusion with the five independent evidence statements. Do not modify protocol, strategy, dates, statistical methods, or acceptance interpretation after seeing results.

- [ ] **Step 4: Extend the sole E2E archive expectation**

Update `test_all_frozen_experiment_archives_validate` to expect 47 archives ending in `0901_EX20`, `0901_EX21`, `0902_EX01`.

- [ ] **Step 5: Validate the experiment and the full archive set**

Run: `\.\.venv\Scripts\czsc-trader.exe archive validate --archive experiments/0902_EX01 --repo-root . --format json`

Run: `\.\.venv\Scripts\czsc-trader.exe archive validate --all --repo-root . --format json`

Expected: both PASS and full count 47.

- [ ] **Step 6: Run the minimal delivery checks**

Run: `\.\.venv\Scripts\python.exe -m pytest -q tests/test_cli_e2e.py -k "active_baseline or archive or robustness or cscv or cyclic or deflated or geometry"`

Run: `git diff --check`

Expected: focused tests PASS and no whitespace errors.

- [ ] **Step 7: Commit the completed archive**

```powershell
git add experiments/0902_EX01 tests/test_cli_e2e.py
git commit -m "research: complete baseline robustness validation"
```

---

### Task 4: Final review and handoff

**Files:**
- Review only: all branch changes.

- [ ] **Step 1: Compare implementation to the design spec**

Confirm every frozen boundary, statistical definition, artifact, and no-optimization rule is represented in code and output.

- [ ] **Step 2: Verify Git state and branch history**

Run: `git status --short --branch`

Run: `git log --oneline master..HEAD`

Expected: clean branch with design, implementation, preregistration, and completed experiment commits.

- [ ] **Step 3: Present the evidence-led result**

Report the five robustness findings, the strongest supporting fact, the strongest limitation, and the experiment/spec paths. Do not merge or push unless the user explicitly asks after seeing the result.
