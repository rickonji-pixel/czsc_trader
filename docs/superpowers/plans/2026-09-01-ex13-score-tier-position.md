# EX13 Score-Tier Position Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Determine whether EX13's continuous score supports causal tiered position sizing and freeze a challenger only if it preserves EX13 return while strictly improving maximum drawdown.

**Architecture:** Add a small pure-function score-tier state machine, then two separate preregistered runners: one for score monotonicity diagnosis and one conditionally enabled strategy challenge. Both reconstruct the immutable EX13 score from its archived identity; a final archive records either a frozen challenger or route termination.

**Tech Stack:** Python 3.12, pandas, NumPy, statsmodels HAC through existing helpers, vectorbt through the existing backtest engine, pytest, Git-tracked experiment archives.

**Spec:** `docs/superpowers/specs/2026-09-01-ex13-score-tier-position-design.md`

## Global Constraints

- Work in the current `codex/0901-czsc-route-decision` branch; do not create a worktree or subagent.
- Freeze EX13's 13 factors, weights, normalization, score formula, event definition, entry threshold `0.175`, exit threshold `0.025`, minimum hold `3`, and next-open execution.
- Use 2020 only for initialization and 2021-01-01 through 2025-12-31 for diagnosis and selection.
- Never load 2026 in diagnosis or candidate selection; only a frozen winner may run the declared retrospective cutoff `2026-08-28`.
- Compare strategy candidates to EX13, not `baseline_20260826`.
- PASS requires return no lower than EX13 within `1e-12`, maximum drawdown strictly better by more than `1e-12`, and all audits PASS.
- Use exactly mappings M1-M4 from the spec and positions in `{0, 0.5, 0.75, 1}`.
- Correct EX15's causal explanation without changing its recorded metrics or promotion decision.

---

### Task 1: Pure score-tier and position state machine

**Files:**
- Create: `src/czsc_trader/score_tier_position.py`
- Create: `tests/test_score_tier_position.py`

**Interfaces:**
- Produces: `classify_score_tiers(scores: pd.Series) -> pd.Series`
- Produces: `build_tier_mapping(candidate_id: str) -> dict[int, float]`
- Produces: `positions_from_score_tiers(scores: pd.Series, rule: Rule, candidate_id: str, resize_confirm_days: int = 2) -> pd.Series`
- Produces: `score_tier_candidate_passes(champion: Mapping[str, object], challenger: Mapping[str, object], return_tolerance: float, drawdown_tolerance: float) -> bool`

- [ ] **Step 1: Write boundary and mapping tests**

```python
def test_classify_score_tiers_uses_frozen_boundaries():
    scores = pd.Series([0.025, 0.026, 0.074, 0.075, 0.124, 0.125, 0.174, 0.175])
    assert classify_score_tiers(scores).tolist() == [0, 1, 1, 2, 2, 3, 3, 4]

def test_only_four_preregistered_mappings_exist():
    assert build_tier_mapping("M1") == {0: 0.0, 1: 0.5, 2: 0.75, 3: 1.0, 4: 1.0}
    with pytest.raises(ValueError):
        build_tier_mapping("M5")
```

- [ ] **Step 2: Run tests and verify import failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_score_tier_position.py -q`  
Expected: FAIL because `czsc_trader.score_tier_position` does not exist.

- [ ] **Step 3: Implement fixed tier classification and mappings**

```python
TIER_MAPPINGS = {
    "M1": {0: 0.0, 1: 0.5, 2: 0.75, 3: 1.0, 4: 1.0},
    "M2": {0: 0.0, 1: 0.5, 2: 0.5, 3: 0.75, 4: 1.0},
    "M3": {0: 0.0, 1: 0.5, 2: 0.75, 3: 0.75, 4: 1.0},
    "M4": {0: 0.0, 1: 0.5, 2: 0.5, 3: 1.0, 4: 1.0},
}

def classify_score_tiers(scores):
    values = scores.astype(float)
    tiers = pd.cut(values, [-np.inf, 0.025, 0.075, 0.125, 0.175, np.inf],
                   labels=[0, 1, 2, 3, 4], right=False, include_lowest=True)
    tiers.loc[values.eq(0.025)] = 0
    return tiers.astype(int).rename("score_tier")
```

- [ ] **Step 4: Write causal state-transition tests**

```python
def test_resize_requires_two_completed_signal_days_and_exit_is_immediate():
    scores = pd.Series([0.20, 0.20, 0.20, 0.05, 0.05, 0.20, 0.20, 0.01])
    target = positions_from_score_tiers(scores, Rule(weights=(0.0, 0.0, 0.0), enter=0.175, exit=0.025,
        confirm_days=1, min_hold_days=3, exit_confirm_days=1, entry_gate="none"), "M1")
    assert target.tolist() == [1, 1, 1, 1, 0.5, 0.5, 1, 0]

def test_fractional_target_never_uses_next_score():
    rule = Rule(weights=(0.0, 0.0, 0.0), enter=0.175, exit=0.025,
                confirm_days=1, min_hold_days=3, exit_confirm_days=1, entry_gate="none")
    scores = pd.Series([0.20, 0.20, 0.08, 0.08, 0.20, 0.01])
    prefix = positions_from_score_tiers(scores.iloc[:-1], rule, "M2")
    full = positions_from_score_tiers(scores, rule, "M2")
    assert prefix.equals(full.iloc[:-1])
```

- [ ] **Step 5: Implement the state machine and hard gate**

Implement one forward loop. Empty-state entry continues to require `score >= rule.enter`; held-state exit continues to require minimum hold and `score <= rule.exit`; nonzero resizing requires two consecutive equal desired tiers. Validate finite scores, `entry_gate == "none"`, mapping identity, and output domain. Implement the PASS predicate using the exact tolerances from Global Constraints.

- [ ] **Step 6: Run tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_score_tier_position.py -q`  
Expected: PASS.  
Commit: `feat: add causal score-tier position state machine`

### Task 2: EX13 score reconstruction and monotonicity statistics

**Files:**
- Create: `src/czsc_trader/score_tier_diagnostic.py`
- Modify: `tests/test_score_tier_position.py`

**Interfaces:**
- Consumes: `classify_score_tiers`
- Produces: `ScoreTierDiagnostic` dataclass containing `summary`, `tier_metrics`, `yearly_metrics`, `hac_result`, and `influence_audit`
- Produces: `diagnose_score_monotonicity(scores: pd.Series, target: pd.Series, event: pd.Series, outcomes: pd.DataFrame) -> ScoreTierDiagnostic`

- [ ] **Step 1: Write synthetic PASS and FAIL tests**

Construct five calendar years with at least 30 held observations in S1/S2 and S4 per year. In the PASS fixture, make S4 `return_20` larger and `max_drawdown_20` less negative in every year. In the FAIL fixture, reverse 2024 and 2025. Assert PASS only for the first fixture, annual direction count `5`, and all leave-one-year-out effects positive.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_score_tier_position.py -q`  
Expected: FAIL because the diagnostic API does not exist.

- [ ] **Step 3: Implement fixed-support tier metrics**

For each tier and year, calculate count, mean 5/10/20-day return, mean 5/10/20-day maximum drawdown, and win rate. The primary contrast is held S4 minus held S1/S2 for `return_20` and `max_drawdown_20`. A positive drawdown contrast means S4 is less negative. Reject years with either side below 30 observations.

- [ ] **Step 4: Implement HAC and influence gates**

Use existing `hac_incremental_test` with ordered tier as candidate and `[target_position, event]` as references, `max_lag=20`. Record the maximum annual absolute-effect share and every leave-one-year-out contrast. PASS requires the five exact conditions from spec section 4.3.

- [ ] **Step 5: Run tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_score_tier_position.py -q`  
Expected: PASS.  
Commit: `feat: add EX13 score monotonicity diagnostics`

### Task 3: Preregister and run the monotonicity experiment

**Files:**
- Create: `src/czsc_trader/score_tier_diagnostic_runner.py`
- Modify: `src/czsc_trader/research/handlers.py`
- Modify: `tests/test_cli_contracts.py`
- Create before performance access: `experiments/0901_EX16/01_goal.md`
- Create before performance access: `experiments/0901_EX16/02_design.md`
- Create before performance access: `experiments/0901_EX16/artifacts/protocol.json`
- Generate after execution: remaining EX16 documents and artifacts

**Interfaces:**
- Produces handler ID `ex13_score_monotonicity_diagnostic`
- Produces `run_score_tier_diagnostic(raw_dir: Path, baseline_root: Path, experiment_dir: Path, *, execution_commit: str) -> dict[str, object]`

- [ ] **Step 1: Write handler propagation and protocol-rejection tests**

Add a handler test matching existing route-handler tests. Add protocol tests rejecting `visible_end != "2025-12-31"`, `access_2026 is not False`, an EX13 manifest hash mismatch, altered boundaries, or altered support count.

- [ ] **Step 2: Run tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_cli_contracts.py tests/test_score_tier_position.py -q`  
Expected: FAIL because runner and handler are absent.

- [ ] **Step 3: Implement replay-only runner**

Reuse EX13's frozen file and `_strategy_inputs`/`_target` reconstruction, but reject data after 2025-12-31. Build outcomes with existing `build_forward_outcomes`. Perform a prefix replay ending 2023-12-31 and require score, tier, event, and target equality to the full-run prefix.

- [ ] **Step 4: Write complete artifacts**

Write `tier_metrics.csv`, `yearly_metrics.csv`, `hac_result.json`, `influence_audit.csv`, `causal_replay_audit.json`, `identity_audit.json`, and `metrics.json`; generate four required Markdown documents and a validated experiment manifest. No candidate positions or backtest results belong in EX16.

- [ ] **Step 5: Commit preregistration before formal execution**

Commit only goal, design, and protocol as `research: preregister EX13 score monotonicity diagnosis`.

- [ ] **Step 6: Execute through the standard CLI**

Run: `.\.venv\Scripts\czsc-trader.exe experiment run --dir experiments/0901_EX16 --repo-root . --format json`  
Expected: PASS command result with experiment status PASS or FAIL and `access_2026=false`.

- [ ] **Step 7: Validate and commit the archive**

Run: `.\.venv\Scripts\czsc-trader.exe archive validate --archive experiments/0901_EX16 --repo-root . --format json`  
Expected: PASS.  
Commit: `research: archive EX13 score monotonicity diagnosis`

### Task 4: Conditionally build and run the four-candidate strategy challenge

**Files:**
- Create only if EX16 PASS: `src/czsc_trader/score_tier_strategy_runner.py`
- Modify only if EX16 PASS: `src/czsc_trader/research/handlers.py`
- Modify only if EX16 PASS: `tests/test_cli_contracts.py`
- Create only if EX16 PASS: `experiments/0901_EX17/`

**Interfaces:**
- Consumes: EX16 PASS manifest and M1-M4 from `build_tier_mapping`
- Produces handler ID `ex13_score_tier_strategy_challenge`
- Produces `run_score_tier_strategy_challenge(...) -> dict[str, object]`

- [ ] **Step 1: If EX16 FAIL, skip all strategy code and proceed to Task 5**

Record `diagnostic_gate_pass=false`; do not create a strategy protocol, runner, or candidate results.

- [ ] **Step 2: If EX16 PASS, write state-path and handler tests**

Assert the runner evaluates exactly `("M1", "M2", "M3", "M4")`, compares against reconstructed EX13, rejects any 2026 selection cutoff, and returns the delegated summary through the standard handler.

- [ ] **Step 3: Implement the candidate runner**

Evaluate EX13 and four target paths over the continuous 2021—2025 window plus individual years. Use `run_period_backtests`, one-way fee `0.0005`, cash `1_000_000`, and the hard gate helper. Rank PASS candidates by less-negative maximum drawdown, return, Sharpe, fewer position changes, then mapping ID.

- [ ] **Step 4: Implement complete audits and outputs**

Write `candidate_results.csv`, `period_comparison.csv`, `fee_sensitivity.csv`, `orders.csv`, `position_path.csv`, `causal_strategy_audit.json`, and `identity_audit.json`. Freeze a self-contained challenger only when a candidate passes; include all 13 factor weights, mapping, confirmation rule, thresholds, and execution rule.

- [ ] **Step 5: Commit EX17 preregistration before execution**

Freeze exact parent hashes, four mappings, fee rates, ranking, PASS tolerances, and `access_2026=false`. Commit as `research: preregister EX13 score-tier challenge`.

- [ ] **Step 6: Execute, validate, and commit EX17**

Run through `czsc-trader experiment run`; validate the archive; commit as `research: archive EX13 score-tier challenge`.

- [ ] **Step 7: Run frozen 2026 replay only for an EX17 winner**

After the frozen challenger file exists, run Q1, H1, and M1-M8 once through cutoff 2026-08-28. Store results as descriptive retrospective evidence; never rerun selection or modify the mapping.

### Task 5: Terminal decision, EX15 correction, and handoff

**Files:**
- Modify: `experiments/0901_EX15/03_execution.md`
- Modify: `experiments/0901_EX15/04_conclusion.md`
- Modify: `experiments/0901_EX15/artifacts/metrics.json`
- Rebuild: `experiments/0901_EX15/experiment_manifest.json`
- Create: next experiment directory (`0901_EX17` if Task 4 skipped, otherwise `0901_EX18`) for terminal decision
- Modify: `docs/RESEARCH_HANDOFF.md`

**Interfaces:**
- Consumes: EX16 and optional EX17 immutable manifests
- Produces final decision `SCORE_TIER_ROUTE_TERMINATED` or `HISTORICAL_CHALLENGER_FOUND`

- [ ] **Step 1: Correct EX15 explanation without altering numerical facts**

Record that 2026 challenger scores equaled `0.8 × champion_score`, all 159 scores differed, seven days changed the entry-threshold classification, and zero target days differed because those seven dates were already held while exit classifications remained identical.

- [ ] **Step 2: Create the terminal archive**

Summarize diagnostic support, candidate funnel, exact parent hashes, historical comparison, audits, and the reason the route stopped. If no strategy ran, say so explicitly. If a winner exists, point to its self-contained frozen file without changing the active registry.

- [ ] **Step 3: Update cross-machine handoff**

Add the new experiments and preserve the rule that Git-tracked archives, not local outputs or terminal text, are the research source of truth.

- [ ] **Step 4: Rebuild every changed manifest and validate all archives**

Run: `.\.venv\Scripts\czsc-trader.exe archive validate --all --repo-root . --format json`  
Expected: PASS with every tracked experiment listed.

- [ ] **Step 5: Run final verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src tests
git diff --check
git status --short --branch
```

Expected: all tests PASS, compileall and diff check exit 0, and no uncommitted files.

- [ ] **Step 6: Commit final evidence**

Commit as `research: conclude EX13 score-tier position route`. Keep the branch unmerged and unpushed for user review.
