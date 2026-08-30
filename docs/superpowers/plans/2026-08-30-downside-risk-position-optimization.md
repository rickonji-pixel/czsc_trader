# Downside-Risk Position Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Select a downside-risk position overlay on 2021-2025 data, freeze it, and report whether its 2026FULL return does not decline while maximum drawdown strictly improves.

**Architecture:** Preserve the registered binary champion as the sole entry/exit authority. A focused downside-risk module converts close prices into a causal stress state and multiplies active champion exposure by one of three preregistered pressure positions. A dedicated runner evaluates all 27 candidates before 2026, freezes only an eligible winner, and then performs one final 2026 evaluation.

**Tech Stack:** Python 3.12, pandas, NumPy, vectorbt, pytest, existing `czsc-trader` research registry and archive framework.

**Spec:** `docs/superpowers/specs/2026-08-30-downside-risk-position-optimization-design.md`

## Global Constraints

- Work on `codex/downside-risk-position-optimization`; do not use git worktrees or subagents.
- Baseline identity comes only from `configs/rule_baselines/registry.json` and must resolve to `baseline_20260826`.
- Do not change champion factors, weights, entry/exit thresholds, state-machine dates, next-open execution, fee `0.0005`, or initial cash `1,000,000`.
- Candidate grid is exactly `L={10,20,40}`, `Q={0.70,0.80,0.90}`, and `P={0.25,0.50,0.75}`.
- Select only on 2021-2025; do not load 2026 until an eligible candidate is frozen and hashed.
- If no candidate is eligible, archive FAIL with `holdout_accessed=false` and do not evaluate 2026.
- PASS on 2026FULL requires challenger return `>=` champion return and challenger maximum drawdown numerically `>` champion maximum drawdown.
- PASS, FAIL, ERROR, and no-eligible-candidate outcomes must remain truthful Git-tracked archives.

---

### Task 1: Formal preregistration

**Files:**
- Create: `experiments/0830_EX01/01_goal.md`
- Create: `experiments/0830_EX01/02_design.md`
- Create: `experiments/0830_EX01/implementation_plan.md`
- Create: `experiments/0830_EX01/artifacts/protocol.json`

**Interfaces:**
- Consumes: the approved design spec and immutable active-baseline identity.
- Produces: a machine-readable protocol declaring handler `downside_risk_position_optimization` and all constants in Global Constraints.

- [ ] **Step 1: Write the human preregistration documents**

Record the single hypothesis, the 2020 warm-up/2021-2025 selection/2026 freeze boundary, the 27 candidates, eligibility constraints, stable ranking, 2026FULL objective, diagnostic windows, audit, and truthful failure behavior.

- [ ] **Step 2: Write the exact protocol**

The protocol must include these exact values:

```json
{
  "experiment_id": "0830_EX01",
  "handler": "downside_risk_position_optimization",
  "status": "PRE_REGISTERED",
  "selection_start": "2021-01-01",
  "selection_end": "2025-12-31",
  "downside_lookbacks": [10, 20, 40],
  "stress_quantiles": [0.7, 0.8, 0.9],
  "pressure_positions": [0.25, 0.5, 0.75],
  "stress_history": 252,
  "holdout_cutoff": "2026-08-28",
  "fee_rate": 0.0005,
  "init_cash": 1000000.0
}
```

Add the exact champion object, five annual selection windows, `2026FULL` main window, three diagnostic windows, selection eligibility, ranking fields, risk metric formula, and observed-data disclosure from the spec.

- [ ] **Step 3: Validate and commit before implementation or market-data evaluation**

Run: `git diff --check`

Run: `.\.venv\Scripts\python.exe -m json.tool experiments\0830_EX01\artifacts\protocol.json`

Commit: `research: preregister downside-risk position optimization`

### Task 2: Causal downside-risk state and target path

**Files:**
- Create: `src/czsc_trader/downside_risk.py`
- Create: `tests/test_downside_risk.py`

**Interfaces:**
- Produces: `DownsideRiskSpec(lookback: int, stress_quantile: float, pressure_position: float)` with deterministic `candidate_id`.
- Produces: `downside_volatility(close: pd.Series, lookback: int) -> pd.Series`.
- Produces: `downside_stress(downside_vol: pd.Series, quantile: float, history: int = 252) -> pd.Series`.
- Produces: `downside_risk_target(baseline_target: pd.Series, stress: pd.Series, pressure_position: float) -> pd.Series`.

- [ ] **Step 1: Write failing formula and leakage tests**

Use a short close series with known log returns and assert:

```python
expected = np.sqrt(252 * np.mean(np.minimum(log_returns[-10:], 0.0) ** 2))
assert downside_volatility(close, 10).iloc[-1] == pytest.approx(expected)
```

Construct 253 risk values where the last value is extreme. Assert the day-253 threshold is computed from the preceding 252 values and that mutating the current value cannot alter its own threshold.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_downside_risk.py -q`

Expected: FAIL because `czsc_trader.downside_risk` does not exist.

- [ ] **Step 3: Implement strict validation and causal calculations**

Validate unique increasing indices, positive finite close prices, `lookback > 1`, `0 < quantile < 1`, `history > 1`, and `0 < pressure_position < 1`. Use log returns, `rolling(lookback).mean()`, `shift(1).rolling(history).quantile(quantile)`, and strict `>` stress comparison. Missing thresholds produce `False`.

- [ ] **Step 4: Write and pass the target-composition test**

For baseline `[0,1,1,1,0]`, stress `[False,False,True,False,True]`, and pressure `0.5`, assert target `[0,1,0.5,1,0]`. Assert stress cannot create exposure when the champion is flat.

- [ ] **Step 5: Commit the pure module and tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_downside_risk.py -q`

Commit: `feat: add causal downside-risk position paths`

### Task 3: Resize events, next-open execution, and audit

**Files:**
- Modify: `src/czsc_trader/downside_risk.py`
- Modify: `tests/test_downside_risk.py`
- Modify only if a failing test requires it: `src/czsc_trader/backtest.py`
- Modify only if a failing test requires it: `src/czsc_trader/audit.py`

**Interfaces:**
- Produces: `build_downside_risk_events(target, baseline_target, scores, downside_vol, stress_threshold, spec) -> pd.DataFrame`.
- Event types remain the existing `Entry`, `Reduce`, `Increase`, and `Exit` contracts.

- [ ] **Step 1: Write a failing transition/provenance test**

Build a path `0 -> 0.5 -> 1 -> 0.5 -> 0` and assert event types `Entry, Increase, Reduce, Exit`, exact before/after positions, risk inputs, and candidate ID. Feed the events to `run_period_backtests` and `audit_no_lookahead`; assert Buy/Sell sides and next-open dates.

- [ ] **Step 2: Run the transition test and verify RED**

Run the named test alone and confirm failure because the event builder is absent.

- [ ] **Step 3: Implement the minimal event builder**

Require aligned indices. Classify `0 -> positive` as Entry, increasing positive exposure as Increase, decreasing positive exposure as Reduce, and `positive -> 0` as Exit. Store `event_id`, `signal_date`, `event_type`, champion factor score, downside volatility, lagged stress threshold, stress flag, candidate parameters, before/after positions, and reason.

- [ ] **Step 4: Preserve exact existing audit behavior**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_downside_risk.py tests\test_dynamic_position_sizing.py tests\test_position_sizing.py -q`

Expected: all tests PASS without weakening next-open or position-transition checks.

- [ ] **Step 5: Commit event/audit behavior**

Commit: `feat: audit downside-risk position transitions`

### Task 4: Protocol, candidate selection, and no-eligible freeze gate

**Files:**
- Create: `src/czsc_trader/downside_risk_runner.py`
- Modify: `tests/test_downside_risk.py`

**Interfaces:**
- Produces: `validate_downside_risk_protocol(protocol: Mapping[str, object]) -> None`.
- Produces: `build_downside_risk_specs(protocol) -> tuple[DownsideRiskSpec, ...]`.
- Produces: `rank_eligible_candidates(rows: pd.DataFrame) -> pd.DataFrame`.
- Produces: `downside_risk_2026_pass(champion: Mapping, challenger: Mapping) -> bool`.

- [ ] **Step 1: Write failing protocol and grid tests**

Load the committed protocol and assert validation succeeds, exactly 27 unique IDs are generated, and mutations to any lookback, quantile, pressure position, ranking field, champion hash, fee, date boundary, or formula raise `ValueError`.

- [ ] **Step 2: Write failing eligibility/ranking tests**

Use literal rows to prove candidates with lower selection return or non-improved drawdown are excluded. For eligible rows, assert ordering by drawdown improvement descending, worst annual return delta descending, trade count ascending, then candidate ID.

- [ ] **Step 3: Write the 2026 objective test**

Assert equality in return is accepted, equality in drawdown is rejected, and both conditions are required.

- [ ] **Step 4: Implement only the tested protocol and ranking functions**

Use exact constants from protocol. Candidate eligibility is:

```python
(challenger_return >= champion_return) and
(challenger_max_drawdown > champion_max_drawdown)
```

- [ ] **Step 5: Run and commit focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_downside_risk.py -q`

Commit: `feat: select eligible downside-risk candidates`

### Task 5: Dedicated formal runner and handler

**Files:**
- Modify: `src/czsc_trader/downside_risk_runner.py`
- Modify: `src/czsc_trader/research/handlers.py`
- Modify: `tests/test_downside_risk.py`

**Interfaces:**
- Produces: `run_downside_risk_experiment(raw_dir, baseline_root, experiment_dir, *, execution_commit, fee_rate=0.0005, init_cash=1_000_000.0) -> dict[str, object]`.
- Registers handler ID `downside_risk_position_optimization`.

- [ ] **Step 1: Write failing handler and ERROR/no-eligible archive tests**

Assert the registry resolves `0830_EX01` to the dedicated handler. In temporary experiment copies, inject no eligible rows and a runtime exception; assert no-eligible becomes validated FAIL with `holdout_accessed=false`, while exceptions become validated ERROR and preserve the true access flag.

- [ ] **Step 2: Implement selection without 2026 access**

Load market data only through 2025-12-31, resolve and audit the champion, evaluate champion and all 27 candidate paths for `2021-2025FULL` plus five independently funded annual windows, write `candidate_results.csv` and `selection_metrics.json`, and apply the exact eligibility/ranking rule.

- [ ] **Step 3: Implement freeze or truthful early FAIL**

If no eligible candidate exists, write execution/conclusion documents, build a FAIL manifest with `holdout_accessed=false`, validate it, and return. Otherwise write and hash `frozen_challenger.json` before calling any 2026 loader.

- [ ] **Step 4: Implement the 2026 final evaluation**

Load through 2026-08-28 only after freeze. Evaluate `2026FULL` and the three diagnostic windows, generate risk/events/orders/position paths, run causal and ledger audits, and write `holdout_metrics.json`. Set PASS only from the 2026FULL return and drawdown conditions.

- [ ] **Step 5: Implement truthful result/error documents and manifest**

Normal completion writes PASS or FAIL. Any exception writes `artifacts/error.json`, ERROR documents, exact `holdout_accessed`, and a validated ERROR manifest before re-raising.

- [ ] **Step 6: Register handler and run focused regression tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_downside_risk.py tests\test_dynamic_position_sizing.py tests\test_position_sizing.py -q`

Commit: `feat: run downside-risk position experiment`

### Task 6: Formal execution, archive, and delivery

**Files:**
- Generate: `experiments/0830_EX01/03_execution.md`
- Generate: `experiments/0830_EX01/04_conclusion.md`
- Generate: `experiments/0830_EX01/experiment_manifest.json`
- Generate: `experiments/0830_EX01/artifacts/*`
- Modify: `tests/test_cli_e2e.py`
- Modify: `docs/RESEARCH_HANDOFF.md`

**Interfaces:**
- Consumes: committed preregistration and tested handler.
- Produces: immutable formal archive and exact 2026 result, or truthful no-eligible/ERROR outcome.

- [ ] **Step 1: Verify implementation before formal data execution**

Run all tests except the archive-count test while `0830_EX01` is intentionally incomplete. Run `git diff --check`, then commit all implementation before the formal run.

- [ ] **Step 2: Execute the formal experiment exactly once**

Run: `.\.venv\Scripts\czsc-trader.exe experiment run --dir experiments\0830_EX01 --repo-root .`

The transport command must complete normally for PASS/FAIL; exceptions must leave a valid ERROR archive.

- [ ] **Step 3: Read and verify the entire generated archive**

Inspect all four documents, manifest, protocol, identity audit, selection metrics, candidate table, optional frozen challenger, optional 2026 metrics, events, orders, and position path before citing any result.

- [ ] **Step 4: Update tracked archive expectations and handoff**

Increment the archive count, append `0830_EX01`, record exact selection and 2026 facts, preserve the active baseline, and state whether 2026 was accessed.

- [ ] **Step 5: Run full final verification**

Run pip check, data validation, active baseline validation, archive validation, full pytest, and `git diff --check`. Every command must exit zero.

- [ ] **Step 6: Commit and push the completed experiment branch**

Commit: `research: archive downside-risk position result`

Push `codex/downside-risk-position-optimization` to origin. Do not merge `master` without a separate user request.
