# Lean Runtime and Test Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the obsolete 23,760-candidate and monthly walk-forward runtime, retain only four supported entrypoints and fixed-rule primitives, and make the single default test suite complete in at most 20 seconds.

**Architecture:** Replace the mixed-responsibility `walk_forward.py` with a small `rules.py` module containing only immutable rule application primitives. Move ordinary backtest output allocation into `output_paths.py`, delete the legacy research entrypoint/orchestrator and its full-search tests, and keep one unmarked pytest suite for supported behavior.

**Tech Stack:** Python 3.12, pandas, NumPy, pytest, Git.

**Spec:** `docs/superpowers/specs/2026-08-24-lean-runtime-and-tests-design.md`

## Global Constraints

- Keep only `prepare_market_data.py`, `run_backtest.py`, `run_experiment.py`, and `run_holdout.py` as user entrypoints.
- Preserve frozen baseline files, backtest behavior, experiment candidate space, and `experiments/0824_EX01` byte-for-byte.
- Do not access the 2026 holdout or rerun research.
- Keep one unmarked `pytest -q` suite; remove slow/network test layering.
- The complete suite must pass twice consecutively in no more than 20 seconds per run.
- No worktree, subagent, parallel-test plugin, cache plugin, or timeout plugin.

---

### Task 1: Extract the fixed-rule runtime

**Files:**
- Create: `src/czsc_trader/rules.py`
- Delete: `src/czsc_trader/walk_forward.py`
- Rename/modify: `tests/test_walk_forward.py` → `tests/test_rules.py`
- Modify: `src/czsc_trader/baselines.py`
- Modify: `src/czsc_trader/backtest_runner.py`
- Modify: `src/czsc_trader/experiments.py`
- Modify: `tests/test_experiments.py`

**Interfaces:**
- Produces unchanged `Rule`, `AppliedRule`, `FACTOR_COLUMNS`, `positions_for_rule`, `build_factor_events`, and `apply_fixed_rule` APIs from `czsc_trader.rules`.
- Deletes every candidate search and monthly walk-forward API.

- [x] **Step 1: Record the characterization baseline**

Run the five retained fixed-rule tests from `tests/test_walk_forward.py` and confirm they pass before moving code: entry confirmation/minimum hold, entry gate, exit confirmation reset, factor events, and fixed-rule application.

- [x] **Step 2: Change retained tests and production imports to `czsc_trader.rules`**

Create `tests/test_rules.py` containing only the five fixed-rule tests. Change production imports in baselines, backtest runner, experiments, and experiment tests. Run collection and verify RED with `ModuleNotFoundError: czsc_trader.rules`.

- [x] **Step 3: Implement the minimal `rules.py`**

Move only the following definitions without behavioral changes:

```python
FACTOR_COLUMNS = ("structure", "trend", "volume_position")

@dataclass(frozen=True)
class Rule: ...

@dataclass(frozen=True)
class AppliedRule: ...

def positions_for_rule(factors: pd.DataFrame, rule: Rule) -> tuple[pd.Series, pd.Series]: ...
def build_factor_events(target, scores, factors, rule) -> pd.DataFrame: ...
def apply_fixed_rule(factors: pd.DataFrame, rule: Rule) -> AppliedRule: ...
```

Delete `walk_forward.py` and the old test file after the retained tests pass from the new module.

- [x] **Step 4: Verify the fixed-rule runtime**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_rules.py tests/test_baselines.py tests/test_backtest_runner.py tests/test_experiments.py -q`

Expected: all pass with no walk-forward collection.

### Task 2: Isolate ordinary backtest output paths and delete legacy research

**Files:**
- Create: `src/czsc_trader/output_paths.py`
- Delete: `src/czsc_trader/research.py`
- Delete: `scripts/run_research.py`
- Delete: `tests/test_research.py`
- Modify: `src/czsc_trader/backtest_runner.py`
- Modify: `tests/test_output_paths.py`

**Interfaces:**
- Produces unchanged `create_output_dir(outputs_root: Path, symbol: str, run_date: date) -> Path` from `czsc_trader.output_paths`.
- Removes `run_research`, `run_dated_research`, `_render_report`, and the old CLI.

- [x] **Step 1: Change output-path tests to the new module**

Retain only the first-revision and increment tests. Import `czsc_trader.output_paths`; remove the legacy `run_dated_research` test. Run and verify RED because the new module does not exist.

- [x] **Step 2: Implement `output_paths.py` and update the backtest runner**

Move `create_output_dir` unchanged, including symbol suffix removal, same-date incrementing, and `exist_ok=False`. Update `backtest_runner.py` to import from `.output_paths`.

- [x] **Step 3: Delete the legacy research runtime and tests**

Delete `research.py`, `run_research.py`, and `test_research.py`. Confirm `rg` finds no runtime import of `czsc_trader.research` and no supported-doc reference to `scripts/run_research.py`.

- [x] **Step 4: Verify output and entrypoint behavior**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_output_paths.py tests/test_backtest_runner.py tests/test_experiment_entrypoints.py -q`

Expected: all pass; ordinary backtest uses R numbers and formal research uses EX numbers.

### Task 3: Remove test layering and update current documentation

**Files:**
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `docs/RESEARCH_HANDOFF.md`

**Interfaces:**
- Produces one test command: `.\.venv\Scripts\python.exe -m pytest -q`.

- [x] **Step 1: Remove pytest layering**

Delete the default `-m 'not slow and not network'` expression and the slow/network marker declarations. Keep `--strict-markers --tb=short`.

- [x] **Step 2: Remove obsolete current documentation**

Delete current instructions for `run_research.py`, 23,760 candidates, and slow/network commands. Describe only the four supported entrypoints and the single test command. Historical specs and plans remain unchanged as historical evidence.

- [x] **Step 3: Verify current references**

Run:

```powershell
rg -n "run_research|run_walk_forward|select_fixed_rule|23,760|czsc_trader\.research|pytest.*-m (slow|network)" README.md docs\RESEARCH_HANDOFF.md scripts src tests pyproject.toml
```

Expected: no matches.

### Task 4: Prove behavior preservation and speed

**Files:**
- Modify: `docs/superpowers/plans/2026-08-24-lean-runtime-and-tests.md` (mark completed steps)

**Interfaces:**
- Produces a clean feature branch with one necessary test suite under the approved budget.

- [x] **Step 1: Verify immutable assets**

Confirm no diff in `configs/rule_baselines/`, `docs/baselines/`, or `experiments/0824_EX01/`. Validate the experiment archive manifest and confirm `holdout_accessed` is false.

- [x] **Step 2: Run the complete suite twice with durations**

Run `.\.venv\Scripts\python.exe -m pytest -q --durations=10` twice in separate processes. Each run must pass with zero deselected tests and wall time no more than 20 seconds.

- [x] **Step 3: Run static and dependency checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src scripts dataflows
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

- [x] **Step 4: Commit the simplification**

Commit the runtime deletion, retained primitives, tests, configuration, and documentation together. Do not merge or push unless explicitly requested.
