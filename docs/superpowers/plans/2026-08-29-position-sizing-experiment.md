# Position Sizing Experiment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, execute, and archive the preregistered `0829_EX01` three-level entry-sizing challenge for `588080.SH`.

**Architecture:** Keep the active baseline's scores and binary timing immutable, add a pure entry-sizing state machine, and generalize the backtest/audit boundaries to causal fractional entry and full exit. A dedicated research runner applies the unchanged return-only champion-challenger protocol and freezes its selected threshold before reading 2026.

**Tech Stack:** Python 3.12, pandas, NumPy, vectorbt, pytest, existing `czsc-trader` CLI and experiment archive framework.

**Spec:** `docs/superpowers/specs/2026-08-29-position-sizing-experiment-design.md`

## Global Constraints

- Work on `codex/position-sizing-experiment`; do not use git worktrees.
- The only baseline registry is `configs/rule_baselines/registry.json`.
- Champion is `baseline_20260826` with canonical SHA-256 `fc22ca5a973f77faf528cdb3efba4900163e18fe0c08cf79234d79ef22f5c822`.
- Freeze factors, weights, entry `0.175`, exit `0.025`, state-machine day counts, next-open execution, fee `0.0005`, and initial cash `1,000,000`.
- Candidate full thresholds are exactly `0.200、0.225、0.250`; partial position is exactly `0.5`.
- Selection and PASS use strategy return only; risk metrics are diagnostics only.
- Formal execution occurs once after preregistration is committed.

---

### Task 1: Fractional transition-only backtest

**Files:**
- Modify: `src/czsc_trader/backtest.py`
- Create: `tests/test_position_sizing.py`

**Interfaces:**
- Consumes: daily `open/close` prices and a target-position series in `[0, 1]`.
- Produces: the existing `run_backtest(...) -> BacktestResult` contract with orders only when the target state changes.

- [ ] **Step 1: Write the failing fractional execution test**

```python
def test_fractional_target_buys_once_and_does_not_rebalance_daily() -> None:
    daily = price_frame(open_values=[100, 100, 120, 120], close_values=[100, 100, 120, 120])
    target = pd.Series([0.0, 0.5, 0.5, 0.0], index=daily.index)
    result = run_backtest(daily, target, fee_rate=0.0005, init_cash=100_000.0)
    assert result.orders["side"].tolist() == ["Buy"]
    assert result.orders.iloc[0]["size"] == pytest.approx(500.0)
    assert result.equity.tolist() == pytest.approx([100_000.0, 99_975.0, 109_975.0, 109_975.0])
```

- [ ] **Step 2: Run the test and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_position_sizing.py::test_fractional_target_buys_once_and_does_not_rebalance_daily -q`

Expected: FAIL because `run_backtest` rejects target `0.5`.

- [ ] **Step 3: Generalize validation and the independent ledger**

Validate finite positions in `[0, 1]`. Convert the shifted execution target to a vectorbt order-size series that contains a target percentage only on the first row or a state transition and `NaN` otherwise. In `_independent_equity`, rebalance at the open only on a state transition: buy the required asset-value delta subject to cash plus fee, or sell the required shares; keep shares unchanged otherwise.

- [ ] **Step 4: Run focused and existing backtest tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_position_sizing.py::test_fractional_target_buys_once_and_does_not_rebalance_daily tests/test_cli_e2e.py::test_backtest_run_publishes_audited_artifacts -q`

Expected: both PASS and the binary baseline return remains unchanged.

### Task 2: Entry-fixed sizing state machine and events

**Files:**
- Create: `src/czsc_trader/position_sizing.py`
- Modify: `tests/test_position_sizing.py`

**Interfaces:**
- Produces: `positions_from_entry_sizing(scores, enter, full, exit_, state_rule, partial_position=0.5) -> pd.Series`.
- Produces: `build_entry_sizing_events(target, scores, enter, full, exit_) -> pd.DataFrame`.

- [ ] **Step 1: Write failing state and event tests**

```python
def test_entry_size_is_frozen_until_exit() -> None:
    scores = score_series([0.0, 0.18, 0.30, 0.10, 0.02])
    target = positions_from_entry_sizing(scores, 0.175, 0.225, 0.025, state_rule())
    assert target.tolist() == [0.0, 0.5, 0.5, 0.5, 0.0]

def test_strong_entry_is_full_and_half_entry_event_is_not_an_exit() -> None:
    scores = score_series([0.23, 0.10, 0.02, 0.18])
    target = positions_from_entry_sizing(scores, 0.175, 0.225, 0.025, state_rule(min_hold_days=2))
    events = build_entry_sizing_events(target, scores, 0.175, 0.225, 0.025)
    assert target.tolist() == [1.0, 1.0, 0.0, 0.5]
    assert events["event_type"].tolist() == ["Entry", "Exit", "Entry"]
    assert events["after_position"].tolist() == [1.0, 0.0, 0.5]
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_position_sizing.py -q`

Expected: FAIL because the position-sizing module does not exist.

- [ ] **Step 3: Implement the minimal pure state machine**

On confirmed cash-state entry, assign `1.0` when `score >= full`, else `partial_position`; preserve that exact positive value through the existing hold/exit logic. Validate `exit < enter < full`, `0 < partial_position < 1`, finite scores, and `entry_gate == "none"`. Build Entry for `0 -> positive` and Exit for `positive -> 0`, recording both thresholds and before/after positions.

- [ ] **Step 4: Run all position-sizing tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_position_sizing.py -q`

Expected: all tests PASS.

### Task 3: Fractional causal audit and period provenance

**Files:**
- Modify: `src/czsc_trader/audit.py`
- Modify: `src/czsc_trader/backtest.py`
- Modify: `tests/test_position_sizing.py`

**Interfaces:**
- Consumes: fractional Entry/Exit events from Task 2.
- Produces: unchanged `audit_no_lookahead(...)` response schema with fractional transition checks.

- [ ] **Step 1: Write a failing end-to-end audit test**

Construct a controlled period with a half-entry event, next-session Buy order, and later full exit. Assert audit `PASS`, then mutate the event `after_position` to `1.0` and assert an `AssertionError`.

- [ ] **Step 2: Run the audit test and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_position_sizing.py::test_fractional_orders_require_exact_transition_provenance -q`

Expected: FAIL because the audit rejects `0.5` or fails to detect the mutated transition.

- [ ] **Step 3: Generalize provenance without weakening timing checks**

Accept finite target values in `[0, 1]`; classify Entry as `previous == 0 and current > 0`, Exit as `previous > 0 and current == 0`, and InitialEntry as `current > 0`. Require event `before_position` and `after_position` to equal the target transition. In period-start provenance, record the actual positive initial target instead of hard-coded `1.0`.

- [ ] **Step 4: Run focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_position_sizing.py -q`

Expected: all position-sizing and audit tests PASS.

### Task 4: Preregistered research runner and CLI handler

**Files:**
- Create: `src/czsc_trader/position_sizing_runner.py`
- Modify: `src/czsc_trader/research/handlers.py`
- Modify: `tests/test_position_sizing.py`

**Interfaces:**
- Produces: `validate_position_sizing_protocol(protocol)` and `run_position_sizing_experiment(raw_dir, baseline_root, experiment_dir, fee_rate=0.0005, init_cash=1_000_000.0)`.
- Registers handler ID `entry_fixed_position_sizing`.

- [ ] **Step 1: Write failing protocol, ranking, and PASS tests**

Use literal candidate rows to prove sorting is `win_count`, minimum delta, median delta, mean delta, candidate ID. Verify protocol drift in thresholds, metrics, position policy, or champion identity is rejected. Verify PASS requires strict return leadership in exactly all three `TARGET_PERIODS`.

- [ ] **Step 2: Run runner tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_position_sizing.py -q`

Expected: FAIL because the runner and handler do not exist.

- [ ] **Step 3: Implement deterministic selection, freeze, holdout, and ERROR finalization**

Load only data through 2025 for selection; apply the resolved champion once; evaluate all three sized paths in the eight fixed half-years; freeze the winner and hash it; then load data through `2026-08-21` and evaluate the three original holdout windows. Write every fixed artifact and human document. On an execution exception, write `artifacts/error.json`, ERROR execution/conclusion documents, build and validate an ERROR manifest, then re-raise.

- [ ] **Step 4: Register the handler and run focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_position_sizing.py -q`

Expected: all focused tests PASS and the registry resolves `entry_fixed_position_sizing`.

### Task 5: Commit preregistration, formally execute once, and archive

**Files:**
- Existing preregistration: `experiments/0829_EX01/01_goal.md`
- Existing preregistration: `experiments/0829_EX01/02_design.md`
- Existing protocol: `experiments/0829_EX01/artifacts/protocol.json`
- Generated formal archive: `experiments/0829_EX01/**`
- Modify after completion: `tests/test_cli_e2e.py`
- Modify after completion: `docs/RESEARCH_HANDOFF.md`

**Interfaces:**
- Consumes: committed protocol and tested handler.
- Produces: immutable validated `0829_EX01` Git-tracked research archive.

- [ ] **Step 1: Commit design and preregistration before results**

Run `git diff --check`, inspect the complete protocol diff, then commit the spec, plan, goal, design, implementation plan, and protocol before running the experiment.

- [ ] **Step 2: Commit tested implementation before formal execution**

Run `.\.venv\Scripts\python.exe -m pytest tests/test_position_sizing.py -q` and the existing baseline backtest test, then commit production code and tests.

- [ ] **Step 3: Execute the formal archive once**

Run: `.\.venv\Scripts\czsc-trader.exe experiment run --dir experiments/0829_EX01 --repo-root .`

Expected: command completes with a PASS transport status while the experiment manifest truthfully records strategy status PASS or FAIL; an actual exception creates status ERROR.

- [ ] **Step 4: Update tracked archive expectations and handoff facts**

Change the archive count/list using the generated experiment ID. Add a concise handoff entry containing the exact formal status, frozen threshold, eight-window selection summary, three-window results, audit status, and the rule that no active baseline changed.

- [ ] **Step 5: Run final verification**

Run: `.\.venv\Scripts\python.exe -m pip check`

Run: `.\.venv\Scripts\czsc-trader.exe data validate --symbol 588080.SH`

Run: `.\.venv\Scripts\czsc-trader.exe baseline validate --version baseline_20260826 --symbol 588080.SH`

Run: `.\.venv\Scripts\czsc-trader.exe archive validate --all`

Run: `.\.venv\Scripts\python.exe -m pytest -q`

Run: `git diff --check`

Expected: every command exits zero; pytest reports no failures; archive validation includes `0829_EX01`.

- [ ] **Step 6: Commit and push the completed branch**

Commit the formal archive, handoff update, and final test expectation. Push `codex/position-sizing-experiment` to `origin` and report the exact commit hashes and formal result without promoting the challenger.
