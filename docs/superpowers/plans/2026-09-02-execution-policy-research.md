# 588080 Execution Policy Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, run, and archive a causal low-attention limit-order execution-policy study for frozen baseline `baseline_20260901`.

**Architecture:** Add one pure execution simulator that keeps target and actual positions separate, uses pre-open daily limit orders and 30-minute first-touch evidence, and exposes deterministic service-level selection. Keep experiment orchestration inside immutable archive `0902_EX02`; freeze the research winner before loading 2026 and leave active baseline configuration unchanged.

**Tech Stack:** Python 3.12, pandas, NumPy, existing CZSC factor/baseline APIs, pytest, Git experiment manifests.

**Spec:** `docs/superpowers/specs/2026-09-02-execution-policy-research-design.md`

## Global Constraints

- Run in the current main session on branch `codex/0902-execution-policy-research`; do not create a worktree or subagent.
- Keep `baseline_20260901` immutable and verify registry identity before research.
- Use 2020—2025 for candidate selection and load 2026 only after the winner file exists and is hashed.
- Require at least 90% first-eligible-day entry-cycle fill rate on the research set.
- Treat entry and exit asymmetrically: entry may remain unfilled; exit uses the next available open-price model.
- Round buy limits down to the 0.001 ETF tick.
- Treat 50,000-share volume participation as report-only.
- Keep all research evidence under `experiments/0902_EX02`; keep `outputs/` unchanged.
- Use focused tests, compile, diff check, and all-archive validation; skip the full slow suite.

---

### Task 1: Pure limit-order execution simulator

**Files:**
- Create: `src/czsc_trader/execution_policy.py`
- Modify: `tests/test_cli_e2e.py`

**Interfaces:**
- Produces: `floor_to_tick(price: float, tick: float = 0.001) -> float`.
- Produces: `average_true_range(daily: pd.DataFrame, window: int = 20) -> pd.Series`.
- Produces: `entry_limit_series(daily: pd.DataFrame, family: str, parameter: float, atr_window: int = 20, tick: float = 0.001) -> pd.Series` indexed by signal date.
- Produces: `ExecutionSimulation` with `equity`, `orders`, `daily_state`, `cycles`, and `metrics`.
- Produces: `simulate_limit_policy(daily: pd.DataFrame, intraday: pd.DataFrame, target_position: pd.Series, entry_limits: pd.Series, fee_rate: float = 0.0005, init_cash: float = 1_000_000.0, diagnostic_quantity: int = 50_000) -> ExecutionSimulation`.

- [ ] **Step 1: Write failing tests for tick rounding and entry limits**

```python
def test_execution_policy_rounds_buy_limit_down_to_etf_tick() -> None:
    from czsc_trader.execution_policy import floor_to_tick

    assert floor_to_tick(1.68899) == pytest.approx(1.688)
    assert floor_to_tick(1.68900) == pytest.approx(1.689)
```

- [ ] **Step 2: Run the rounding test and confirm an import failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_cli_e2e.py::test_execution_policy_rounds_buy_limit_down_to_etf_tick -q`

Expected: FAIL because `czsc_trader.execution_policy` does not exist.

- [ ] **Step 3: Implement tick rounding, causal ATR, and both candidate formula families**

Use integer tick arithmetic for rounding. Normalize daily dates, require positive OHLC values, compute True Range from current high/low and previous close, and preserve `NaN` during ATR warmup. Return a signal-date limit series; the simulator applies it on the following session.

- [ ] **Step 4: Add failing tests for open fill, first 30-minute touch, no fill, retry, and exit**

```python
def test_execution_policy_distinguishes_target_and_actual_position() -> None:
    from czsc_trader.execution_policy import simulate_limit_policy

    # Day 2 opens above the cap and never touches it; Day 3 touches the new cap.
    # The actual position stays zero on Day 2, buys on Day 3, and exits at the
    # next available open after the target returns to zero.
    result = simulate_limit_policy(daily, intraday, target, limits)
    assert result.daily_state.loc[dates[1], "actual_position"] == 0.0
    assert result.daily_state.loc[dates[2], "actual_position"] == 1.0
    assert result.orders["side"].tolist() == ["Buy", "Sell"]
```

Build the fixture inline with four daily rows and eight 30-minute bars per day. Assert the first-touch timestamp and conservative limit-price fill for the buy order.

- [ ] **Step 5: Implement the state machine and independent cash/share ledger**

The loop evaluates the target known from the previous session, recalculates a buy limit on every pending-entry day, updates actual position only after a simulated fill, marks equity at the daily close, records original cycle start and retry count, and sells at the first eligible daily open. Orders use the existing columns `signal_date`, `execution_date`, `side`, `size`, `price`, and `fees`, plus execution-specific fields.

- [ ] **Step 6: Run all Task 1 tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_cli_e2e.py -q -k "execution_policy"`

Expected: all execution-policy tests PASS.

- [ ] **Step 7: Commit Task 1**

```powershell
git add src/czsc_trader/execution_policy.py tests/test_cli_e2e.py
git commit -m "feat: add limit order execution simulator"
```

### Task 2: Deterministic service-level selection and metrics

**Files:**
- Modify: `src/czsc_trader/execution_policy.py`
- Modify: `tests/test_cli_e2e.py`

**Interfaces:**
- Consumes: `ExecutionSimulation` from Task 1.
- Produces: `policy_metrics(simulation: ExecutionSimulation, init_cash: float) -> dict[str, float | int | None]`.
- Produces: `select_execution_candidate(candidates: pd.DataFrame, minimum_t1_fill_rate: float = 0.90) -> pd.Series`.

- [ ] **Step 1: Write a failing selector test**

```python
def test_execution_policy_selector_enforces_service_level_then_price() -> None:
    from czsc_trader.execution_policy import select_execution_candidate

    candidates = pd.DataFrame([
        {"candidate_id": "cheap", "t1_fill_rate": 0.89, "cap_p95": 0.001, "cap_mean": 0.0, "worst_calmar": 3.0, "worst_drawdown": -0.05, "family_rank": 0, "parameter": 0.0},
        {"candidate_id": "fixed", "t1_fill_rate": 0.91, "cap_p95": 0.010, "cap_mean": 0.005, "worst_calmar": 1.0, "worst_drawdown": -0.10, "family_rank": 0, "parameter": 0.5},
        {"candidate_id": "atr", "t1_fill_rate": 0.95, "cap_p95": 0.012, "cap_mean": 0.004, "worst_calmar": 2.0, "worst_drawdown": -0.08, "family_rank": 1, "parameter": 0.5},
    ])
    assert select_execution_candidate(candidates)["candidate_id"] == "fixed"
```

- [ ] **Step 2: Run the selector test and confirm the missing-function failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_cli_e2e.py::test_execution_policy_selector_enforces_service_level_then_price -q`

Expected: FAIL because the selector is missing.

- [ ] **Step 3: Implement metrics and lexicographic selection**

Validate required columns, reject a service level outside `[0, 1]`, filter to `t1_fill_rate >= minimum_t1_fill_rate`, and raise `ValueError` when none qualifies. Sort by `cap_p95` ascending, `cap_mean` ascending, `worst_calmar` descending, `worst_drawdown` descending, `family_rank` ascending, `parameter` ascending, and `candidate_id` ascending using stable sorting.

Compute equity metrics with `strategy_comparison_metrics`, daily Sharpe on 252 sessions, cycle fill rates, wait durations, missed cycles, entry price improvements, exit delays, and 50,000-share first-touch participation.

- [ ] **Step 4: Run focused execution-policy tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_cli_e2e.py -q -k "execution_policy"`

Expected: all focused tests PASS.

- [ ] **Step 5: Commit Task 2**

```powershell
git add src/czsc_trader/execution_policy.py tests/test_cli_e2e.py
git commit -m "feat: select execution policy by fill service"
```

### Task 3: Pre-registered experiment runner

**Files:**
- Create: `experiments/0902_EX02/01_goal.md`
- Create: `experiments/0902_EX02/02_design.md`
- Create: `experiments/0902_EX02/run_experiment.py`
- Create: `experiments/0902_EX02/artifacts/protocol.json`
- Modify: `tests/test_cli_e2e.py`

**Interfaces:**
- Consumes: active baseline resolver, market-data loader, factor generator, baseline execution adapter, Task 1 simulator, Task 2 selector, experiment manifest helpers.
- Produces: research artifacts `candidate_metrics.csv`, `candidate_year_metrics.csv`, `selected_research_orders.csv`, `selected_research_cycles.csv`, `selected_research_daily.csv`, `frozen_execution_policy.json`, `test_metrics.json`, `test_orders.csv`, `test_cycles.csv`, `test_daily.csv`, `identity_audit.json`, and `metrics.json`.

- [ ] **Step 1: Write the experiment identity and freeze-order test**

Add a focused test that imports `experiments/0902_EX02/run_experiment.py` by path and calls a small public `assert_freeze_before_test(frozen_path, test_accessed)` guard. Assert a missing frozen file raises and an existing file allows test access.

- [ ] **Step 2: Run the guard test and confirm the file/import failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_cli_e2e.py -q -k "execution_experiment"`

Expected: FAIL because the runner does not exist.

- [ ] **Step 3: Create the pre-registration documents and protocol**

Protocol values must include:

```json
{
  "schema_version": 1,
  "experiment_id": "0902_EX02",
  "status": "PRE_REGISTERED",
  "symbol": "588080.SH",
  "baseline": {"version": "baseline_20260901", "sha256": "711254af3fe951cc0eb32c81121ef52233a0cf2577f46b14683b2f3e6b961993"},
  "research_start": "2020-01-01",
  "research_end": "2025-12-31",
  "test_start": "2026-01-01",
  "test_end": "2026-09-01",
  "minimum_t1_fill_rate": 0.9,
  "fee_rate": 0.0005,
  "init_cash": 1000000.0,
  "diagnostic_quantity": 50000,
  "tick": 0.001,
  "fixed_premium_start": -0.02,
  "fixed_premium_stop": 0.05,
  "fixed_premium_step": 0.0025,
  "atr_window": 20,
  "atr_multiplier_start": -0.5,
  "atr_multiplier_stop": 2.0,
  "atr_multiplier_step": 0.125,
  "access_2026_after_freeze_only": true
}
```

The goal and design documents must state that performance metrics are report-only behind the fill-service selection and that active baseline files remain unchanged.

- [ ] **Step 4: Implement the research runner**

Load only through 2025, generate the active baseline target, enumerate 29 fixed candidates and 21 ATR candidates, simulate each, calculate whole-period and effective-year metrics, select one candidate, write and hash `frozen_execution_policy.json`, call `assert_freeze_before_test`, then load 2026 and run the frozen rule once.

The frozen file stores the formula family, parameter, ATR window, tick, fee rate, warning-gap q05, entry order type, exit primary/fallback types, research service level, baseline identity, and research data hashes.

Before and after execution, hash the active registry, active baseline file, 588080 manifest/validation, and source experiment `0901_EX20` frozen challenger. Raise if any changes.

- [ ] **Step 5: Run runner-focused tests without executing the formal experiment**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_cli_e2e.py -q -k "execution_policy or execution_experiment"`

Expected: all focused tests PASS and no `experiment_manifest.json` exists yet.

- [ ] **Step 6: Commit implementation and pre-registration before test access**

```powershell
git add src/czsc_trader/execution_policy.py tests/test_cli_e2e.py experiments/0902_EX02 docs/superpowers/plans/2026-09-02-execution-policy-research.md
git commit -m "research: preregister execution policy study"
```

### Task 4: Formal run, archive, and handoff

**Files:**
- Generate: `experiments/0902_EX02/03_execution.md`
- Generate: `experiments/0902_EX02/04_conclusion.md`
- Generate: `experiments/0902_EX02/experiment_manifest.json`
- Generate: `experiments/0902_EX02/artifacts/*`
- Modify: `tests/test_cli_e2e.py`
- Modify: `docs/RESEARCH_HANDOFF.md`

**Interfaces:**
- Consumes: committed pre-registration and runner from Task 3.
- Produces: one immutable COMPLETE, FAIL, or ERROR archive and a research recommendation without changing the active baseline.

- [ ] **Step 1: Record the clean committed execution identity**

Run: `git status --short` and require no output. Record `git rev-parse HEAD` in the formal artifacts.

- [ ] **Step 2: Execute the formal experiment exactly once**

Run: `.\.venv\Scripts\python.exe experiments\0902_EX02\run_experiment.py`

Expected: exit 0 for COMPLETE/FAIL, with `locked_test_accessed=true` only after a frozen policy hash exists. Exceptions exit nonzero and still produce an ERROR archive.

- [ ] **Step 3: Inspect the machine evidence before interpreting it**

Read `metrics.json`, `candidate_metrics.csv`, `frozen_execution_policy.json`, `test_metrics.json`, and `identity_audit.json`. Confirm the selected candidate met the 90% research service level, the frozen hash matches, 2026 was accessed after freezing, and critical identities are unchanged.

- [ ] **Step 4: Update the archive-count assertion and handoff**

Change `test_all_frozen_experiment_archives_validate` to expect 48 archives ending in `0901_EX21`, `0902_EX01`, `0902_EX02`. Add a concise handoff section with the selected formula, research fill rate, 2026 fill rate, strategy metric comparison, formal status, and explicit statement that no active execution policy was promoted.

- [ ] **Step 5: Rebuild the manifest after tracked documentation changes**

Use `build_experiment_manifest` only for files inside `0902_EX02`; validate that archive and then every archive. Existing archive manifests remain byte-identical.

- [ ] **Step 6: Run final focused verification**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_cli_e2e.py -q -k "execution_policy or execution_experiment or active_baseline_is_candidate143 or all_frozen_experiment_archives_validate"
.\.venv\Scripts\python.exe -m compileall -q src experiments\0902_EX02
git diff --check
```

Expected: all selected tests PASS, compile exit 0, diff check exit 0, and all 48 archives validate.

- [ ] **Step 7: Commit the completed experiment**

```powershell
git add experiments/0902_EX02 tests/test_cli_e2e.py docs/RESEARCH_HANDOFF.md
git commit -m "research: complete execution policy study"
```
