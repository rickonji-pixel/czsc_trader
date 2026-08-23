# 588080 Absolute Return Targets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Select and audit one fixed CZSC-only rule against the absolute net-return thresholds 10.5%, 82.5%, and 60.0% for the three 2026 windows.

**Architecture:** A shared objectives module owns period definitions and inclusive PASS logic. The fixed-rule selector expands the causal state machine, deduplicates equivalent target histories, and ranks candidates by target margins; backtesting remains execution-only, while a separate diagnostics module explains completed round trips without influencing positions or selection.

**Tech Stack:** Python 3.12, pandas 3.0.5, numpy 2.5.2, CZSC 1.0.1, vectorbt 1.1.0, pytest 9.x.

**Spec:** `docs/superpowers/specs/2026-08-23-absolute-return-targets-design.md`

## Global Constraints

- Only `data/raw` 30-minute, daily, and weekly bars may be read.
- Every position must come directly from the fixed CZSC factor state machine.
- Signal day T executes at the immediately following trading-day open.
- Positions are long/cash only, exactly 0 or 1, with no overlays or date branches.
- The three independent portfolios start with CNY 1,000,000 and pay 0.05% per side.
- PASS requires all three inclusive thresholds: Q1 0.105, H1 0.825, M1-M8 0.600.
- Buy & Hold, turnover, and churn diagnostics do not influence PASS or candidate ranking.
- Preserve R05 and the existing portable baseline until the new result is reviewed.

---

### Task 1: Centralize research objectives

**Files:**
- Create: `src/czsc_trader/objectives.py`
- Create: `tests/test_objectives.py`
- Modify: `src/czsc_trader/research.py`

**Interfaces:**
- Produces: `TARGET_PERIODS: dict[str, tuple[pd.Timestamp, pd.Timestamp]]`
- Produces: `RETURN_TARGETS: dict[str, float]`
- Produces: `evaluate_return(name: str, strategy_return: float) -> dict[str, float | bool]`
- Produces: `overall_pass(windows: dict[str, dict[str, object]]) -> bool`

- [ ] **Step 1: Write failing tests for inclusive thresholds and Buy & Hold independence**

```python
def test_return_equal_to_target_passes():
    assert evaluate_return("2026Q1", 0.105)["pass"] is True

def test_overall_pass_requires_all_three_targets():
    windows = {name: {"strategy_return": target} for name, target in RETURN_TARGETS.items()}
    assert overall_pass(windows) is True
    windows["2026M1-M8"]["strategy_return"] = 0.599999
    assert overall_pass(windows) is False
```

- [ ] **Step 2: Run `python -m pytest tests/test_objectives.py -q` and verify import failure**
- [ ] **Step 3: Implement immutable period/target constants and the two evaluation functions**
- [ ] **Step 4: Run the objective tests and verify PASS**
- [ ] **Step 5: Replace `research.py` local period constants with imports from `objectives.py`**
- [ ] **Step 6: Commit with `git commit -m "feat: define absolute return objectives"`**

### Task 2: Expand the fixed causal rule family

**Files:**
- Modify: `src/czsc_trader/walk_forward.py`
- Modify: `tests/test_walk_forward.py`

**Interfaces:**
- Extends: `Rule` with `exit_confirm_days: int = 1` and `entry_gate: str = "none"`
- Produces: `CANDIDATES` from the exact grid in the spec
- Produces: `candidate_complexity(rule: Rule) -> int`
- Produces: deduplicated candidate histories inside `select_fixed_rule`

- [ ] **Step 1: Write failing tests for structure/trend entry gates**

```python
def test_entry_gate_requires_selected_factor_confirmation():
    rule = Rule((0.4, 0.4, 0.2), 0.2, 0.0, 1, 1, 1, "trend")
    positions, _ = positions_for_rule(factors_with_positive_score_but_negative_trend, rule)
    assert positions.iloc[0] == 0.0
```

- [ ] **Step 2: Run the focused gate test and verify constructor/behavior failure**
- [ ] **Step 3: Implement gate evaluation in the entry confirmation condition**
- [ ] **Step 4: Write failing tests for consecutive exit confirmation and reset**

```python
def test_exit_confirmation_resets_when_score_recovers():
    rule = Rule((0.4, 0.4, 0.2), 0.2, 0.0, 1, 1, 2, "none")
    positions, _ = positions_for_rule(exit_scores_with_one_day_recovery, rule)
    assert positions.tolist() == [1.0, 1.0, 1.0, 1.0, 0.0]
```

- [ ] **Step 5: Run the focused exit test and verify failure for missing behavior**
- [ ] **Step 6: Implement an exit counter that resets above the exit threshold and before minimum-hold eligibility**
- [ ] **Step 7: Replace the old 144-rule grid with the exact 23,760-rule declaration and stable rule IDs**
- [ ] **Step 8: Write a failing test proving equivalent target histories retain the least-complex representative**
- [ ] **Step 9: Implement target-byte hashing and deterministic deduplication before price evaluation**
- [ ] **Step 10: Run `python -m pytest tests/test_walk_forward.py -q` and verify PASS**
- [ ] **Step 11: Commit with `git commit -m "feat: expand fixed CZSC rule family"`**

### Task 3: Rank by absolute target margins

**Files:**
- Modify: `src/czsc_trader/walk_forward.py`
- Modify: `tests/test_walk_forward.py`

**Interfaces:**
- Consumes: `RETURN_TARGETS`
- Candidate columns: `<period>_target_return`, `<period>_target_margin`, `pass_count`, `min_target_margin`, `mean_target_margin`
- Ranking excludes: `turnover`, Buy & Hold, and all churn diagnostics

- [ ] **Step 1: Replace the existing excess-return ranking test with a failing absolute-margin test**

```python
def test_candidate_ranking_prioritizes_worst_target_margin():
    candidates = pd.DataFrame([
        {"rule_id": "fragile", "pass_count": 2, "min_target_margin": -0.01, "mean_target_margin": 0.30, "max_drawdown": -0.1, "complexity": 2},
        {"rule_id": "robust", "pass_count": 3, "min_target_margin": 0.01, "mean_target_margin": 0.02, "max_drawdown": -0.2, "complexity": 4},
    ])
    assert rank_candidate_results(candidates).iloc[0]["rule_id"] == "robust"
```

- [ ] **Step 2: Run the focused test and verify failure on missing target-margin columns**
- [ ] **Step 3: Implement margin calculation, inclusive pass counts, and the approved stable ranking keys**
- [ ] **Step 4: Add a test showing turnover changes cannot change ranking**
- [ ] **Step 5: Run all walk-forward tests and verify PASS**
- [ ] **Step 6: Commit with `git commit -m "feat: rank candidates by return targets"`**

### Task 4: Apply the unified PASS policy to backtests and reports

**Files:**
- Modify: `src/czsc_trader/backtest.py`
- Modify: `src/czsc_trader/research.py`
- Modify: `tests/test_backtest.py`
- Modify: `tests/test_research.py`

**Interfaces:**
- Period metrics add: `target_return`, `target_margin`, `pass`
- `metrics.json.overall_pass` comes only from `objectives.overall_pass`

- [ ] **Step 1: Write a failing backtest test that changes Buy & Hold while holding strategy return fixed and expects unchanged PASS**
- [ ] **Step 2: Run the focused test and verify the current Buy & Hold comparison fails it**
- [ ] **Step 3: Pass period names into objective evaluation and populate target fields**
- [ ] **Step 4: Write failing research assertions for targets, margins, and inclusive overall PASS**
- [ ] **Step 5: Update report tables, `metrics.json`, and manifest acceptance-policy metadata**
- [ ] **Step 6: Run backtest and research tests and verify PASS**
- [ ] **Step 7: Commit with `git commit -m "feat: report absolute return acceptance"`**

### Task 5: Add observational trade diagnostics

**Files:**
- Create: `src/czsc_trader/diagnostics.py`
- Create: `tests/test_diagnostics.py`
- Modify: `src/czsc_trader/research.py`
- Modify: `tests/test_research.py`

**Interfaces:**
- Produces: `build_trade_diagnostics(orders: pd.DataFrame, trading_dates: pd.DatetimeIndex) -> pd.DataFrame`
- Produces columns: period, entry/exit signal and execution dates, holding days, prices, fees, net return, one-signal-day exit, July-August flag
- Diagnostics consume completed orders only and return no position series

- [ ] **Step 1: Write failing tests for pairing buy/sell orders and fee-inclusive net return**
- [ ] **Step 2: Run `python -m pytest tests/test_diagnostics.py -q` and verify import failure**
- [ ] **Step 3: Implement deterministic FIFO round-trip pairing for long/cash orders**
- [ ] **Step 4: Add failing tests for one-signal-day exits and July-August classification**
- [ ] **Step 5: Implement the diagnostic flags and summary counts**
- [ ] **Step 6: Export `trade_diagnostics.csv` and add report summaries without feeding diagnostics into selection**
- [ ] **Step 7: Run diagnostics and research tests and verify PASS**
- [ ] **Step 8: Commit with `git commit -m "feat: export trade churn diagnostics"`**

### Task 6: Documentation, full verification, and formal research run

**Files:**
- Modify: `README.md`
- Modify: `docs/RESEARCH_HANDOFF.md`
- Preserve: `docs/baselines/588080_2026_expected.json`

**Interfaces:**
- Formal output: a new `outputs/588080_0823_RXX` directory
- Required evidence: selected rule, candidate results, three order/equity/chart sets, diagnostics, metrics, manifest, report, audit PASS

- [ ] **Step 1: Update README and handoff to distinguish the R05 baseline from the new absolute-return experiment**
- [ ] **Step 2: Run `python -m pytest -q` and require zero failures**
- [ ] **Step 3: Run `python -m compileall -q src tests scripts`, `git diff --check`, and `python -m pip check`**
- [ ] **Step 4: Run `python scripts/run_research.py` once, using the returned output directory**
- [ ] **Step 5: Inspect `metrics.json`, `selected_rule.json`, `candidate_results.csv`, `trade_diagnostics.csv`, and all orders**
- [ ] **Step 6: Confirm audit PASS independently and report each return against its target, including gaps on FAIL**
- [ ] **Step 7: Commit code and documentation with `git commit -m "docs: document absolute return research"`**

