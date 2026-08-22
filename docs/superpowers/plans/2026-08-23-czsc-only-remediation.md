# CZSC-Only Strategy Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the non-factor Q1 alpha lock with one fixed, auditable CZSC-only factor rule whose every order has explicit factor provenance.

**Architecture:** CZSC raw signals are converted into three daily grouped factors. A fixed-rule candidate selector evaluates deterministic long/cash state machines over the three independently funded periods and selects lexicographically by pass count, worst excess, mean excess, turnover, drawdown, and simplicity. Factor events are the sole source of target positions; backtest orders carry event identifiers and a strict audit rejects unmatched trades.

**Tech Stack:** Python 3.12, CZSC 1.0.1, pandas 3, vectorbt 1.1.0, Plotly 6.9.0, pytest 8, Git local branch without worktrees.

**Spec:** `docs/superpowers/specs/2026-08-23-czsc-only-remediation-design.md`

## Global Constraints

- Read market data only from `data/raw`.
- A signal dated `T` may execute only at the next trading session's open.
- Final target position must equal the CZSC factor state machine output; no performance, benchmark, date, or equity overlay is allowed.
- Each period starts with 1,000,000 cash and zero holdings; a first-open order may align to the prior trading day's active factor target.
- All development runs in `D:\CodeBase\czsc_trader` on local branch `research/czsc-only-remediation`; never create or use a worktree.
- A compliant FAIL is acceptable; constraints may not be weakened to force PASS.

---

### Task 1: Expand CZSC Structure Signals

**Files:**
- Modify: `src/czsc_trader/factors.py`
- Modify: `tests/test_factors.py`

**Interfaces:**
- Consumes: `MarketData` and `czsc.generate_czsc_signals`.
- Produces: `generate_factor_frame(data: MarketData) -> FactorResult` with five/seven-bi classifications included in `structure`.

- [ ] **Step 1: Write the failing test**

Add assertions that `DAILY_CONFIG` contains `cxt_five_bi_V230619` and `cxt_seven_bi_V230620`, and that `_signal_score` maps `底背驰` to `1.0` and `顶背驰` to `-1.0`.

```python
def test_divergence_signals_are_structure_inputs() -> None:
    names = {item["name"] for item in factors.DAILY_CONFIG}
    assert {"cxt_five_bi_V230619", "cxt_seven_bi_V230620"} <= names
    unknown = Counter()
    assert factors._signal_score("底背驰_任意_任意_0", unknown) == 1.0
    assert factors._signal_score("顶背驰_任意_任意_0", unknown) == -1.0
```

- [ ] **Step 2: Run test to verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_factors.py::test_divergence_signals_are_structure_inputs -q`

Expected: FAIL because the configs and mappings are absent.

- [ ] **Step 3: Implement minimal signal additions**

Append the two daily configs and extend `_signal_score` so bottom/top divergence is scored before neutral fallback.

- [ ] **Step 4: Run focused and factor causality tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_factors.py tests/test_data.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/czsc_trader/factors.py tests/test_factors.py
git commit -m "feat: add CZSC divergence factor inputs"
```

### Task 2: Select One Fixed CZSC Rule and Emit Factor Events

**Files:**
- Modify: `src/czsc_trader/walk_forward.py`
- Replace tests: `tests/test_walk_forward.py`

**Interfaces:**
- Produces `FixedSelectionResult(target_position, scores, events, candidates, rule)`.
- Produces `select_fixed_rule(daily, factors, periods, fee_rate=0.0005) -> FixedSelectionResult`.
- Produces `build_factor_events(target, scores, factors, rule) -> pd.DataFrame`.
- Removes `AlphaLockResult`, `apply_annual_alpha_lock`, and `apply_completed_q1_alpha_lock`.

- [ ] **Step 1: Write failing tests for forbidden APIs and event state transitions**

```python
def test_factor_events_are_the_only_position_transitions() -> None:
    target, scores = positions_for_rule(factors, rule)
    events = build_factor_events(target, scores, factors, rule)
    assert events["after_position"].tolist() == [1.0, 0.0]
    assert events["event_type"].tolist() == ["Entry", "Exit"]
    assert events["signal_date"].tolist() == list(target.index[target.ne(target.shift()).fillna(False)])

def test_performance_overlay_apis_do_not_exist() -> None:
    assert not hasattr(walk_forward, "apply_annual_alpha_lock")
    assert not hasattr(walk_forward, "apply_completed_q1_alpha_lock")
```

- [ ] **Step 2: Run tests to verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_walk_forward.py -q`

Expected: FAIL because event and fixed-selection APIs are absent and lock APIs still exist.

- [ ] **Step 3: Implement event generation and fixed candidate selection**

Use the existing deterministic `positions_for_rule`. Evaluate each statically declared candidate with independently reset ledgers for all target periods. Build a candidate row with `pass_count`, `min_excess`, `mean_excess`, `turnover`, `max_drawdown`, and `complexity`; sort descending on the first three and ascending on the last three. Return the first candidate and its single target series.

- [ ] **Step 4: Run focused tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_walk_forward.py -q`

Expected: PASS, including deterministic tie-break tests and future-factor truncation tests.

- [ ] **Step 5: Commit**

```powershell
git add src/czsc_trader/walk_forward.py tests/test_walk_forward.py
git commit -m "feat: select fixed CZSC factor rule"
```

### Task 3: Attach Factor Provenance to Every Order

**Files:**
- Modify: `src/czsc_trader/backtest.py`
- Modify: `src/czsc_trader/audit.py`
- Modify: `tests/test_backtest.py`
- Modify: `tests/test_audit.py`

**Interfaces:**
- `run_period_backtests(..., factor_events: pd.DataFrame, factor_frame: pd.DataFrame) -> dict[str, PeriodBacktestResult]`.
- Every order adds `factor_event_id` and `event_type`.
- `audit_no_lookahead(orders, factor_events, target_position, factor_frame) -> dict`.

- [ ] **Step 1: Write failing provenance tests**

Create a tiny target series with one regular transition and one period-first alignment. Assert the regular order references the matching `Entry`/`Exit`, while the first-open order gets an `InitialEntry` record based on the prior signal date.

- [ ] **Step 2: Run tests to verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_backtest.py tests/test_audit.py -q`

Expected: FAIL because orders lack provenance fields and audit accepts unmatched orders.

- [ ] **Step 3: Implement provenance attachment and strict audit**

Join ordinary orders to events by `signal_date` and expected direction. For first-open buys whose prior target is one, synthesize a stable identifier `InitialEntry:<period>:<signal-date>`. Reject missing, duplicate, directionally inconsistent, same-day, or score-mismatched provenance.

- [ ] **Step 4: Run focused tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_backtest.py tests/test_audit.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/czsc_trader/backtest.py src/czsc_trader/audit.py tests/test_backtest.py tests/test_audit.py
git commit -m "feat: audit factor provenance for every order"
```

### Task 4: Rebuild Research Outputs and Charts Around the Compliant Strategy

**Files:**
- Modify: `src/czsc_trader/research.py`
- Modify: `src/czsc_trader/charting.py`
- Modify: `tests/test_research.py`
- Modify: `tests/test_charting.py`
- Modify: `README.md`

**Interfaces:**
- `run_research` writes `factor_events.csv`, `candidate_results.csv`, and `selected_rule.json`.
- It no longer writes `monthly_parameters.csv` or `alpha_locks.csv`.
- `build_period_chart` renders factor events and links strategy trades by `factor_event_id`.

- [ ] **Step 1: Write failing artifact and chart tests**

Assert the new files exist, forbidden alpha-lock artifacts and manifest keys are absent, report labels the result as 2026 sample-optimized, all orders have factor event IDs, and chart HTML includes `周期初始因子入场` when applicable.

- [ ] **Step 2: Run tests to verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_research.py tests/test_charting.py -q`

Expected: FAIL on the old alpha-lock pipeline.

- [ ] **Step 3: Implement compliant orchestration and rendering**

Call `select_fixed_rule`, backtest exactly that target, attach provenance, audit, export the selected rule/candidate table/events, and render three offline charts. Report PASS/FAIL from measured windows without asserting that all must pass.

- [ ] **Step 4: Run integration tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_research.py tests/test_charting.py tests/test_output_paths.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/czsc_trader/research.py src/czsc_trader/charting.py tests/test_research.py tests/test_charting.py README.md
git commit -m "feat: export compliant CZSC-only research"
```

### Task 5: Formal Research Run, Visual Inspection, and Integration

**Files:**
- Generate ignored output: `outputs/588080_0823_RXX/`
- Modify documentation only if verification exposes an inaccurate statement.

**Interfaces:**
- Formal command: `.venv\Scripts\python.exe scripts\run_research.py`.

- [ ] **Step 1: Run full verification**

```powershell
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m compileall -q src tests scripts
git diff --check
```

Expected: all commands exit zero.

- [ ] **Step 2: Run formal research**

Run: `.venv\Scripts\python.exe scripts\run_research.py`

Expected: new revision directory, audit PASS, three measured windows, and no alpha-lock artifacts.

- [ ] **Step 3: Inspect all three HTML charts in the local browser**

Verify each chart renders, has no console errors, and visually distinguishes factor signals, initial alignment, and actual transactions.

- [ ] **Step 4: Verify repository and output evidence**

Confirm every order has a valid `factor_event_id`, selected rule is fixed across all dates, raw hashes match, and any FAIL is reported without a constraint exception.

- [ ] **Step 5: Commit verification-only documentation changes if any, merge and push**

Merge the local feature branch into `master` only after fresh verification, then push with the configured SSH URL and confirm local/remote SHA equality.
