# Regime-Conditioned Weight Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, execute, and archive 0901_EX19, testing whether two causal ER60 regimes with group-level conditional weights improve maximum drawdown, Calmar ratio, and win/loss ratio versus `baseline_20260826`.

**Architecture:** Add one pure module for lagged ER60 labels, deterministic group-weight projection, regime-aware scoring, closed-trade accounting, and risk-quality gates. Add one experiment runner that evaluates the frozen 625-candidate grid on 2021—2025, freezes a winner before any 2026 load, conditionally runs the locked 2026 test, and emits the standard auditable experiment archive through the existing CLI handler registry.

**Tech Stack:** Python 3.12, pandas, NumPy, vectorbt through the existing backtest API, pytest, existing `czsc-trader` research/archive CLI.

**Spec:** `docs/superpowers/specs/2026-09-01-regime-conditioned-weight-design.md`

## Global Constraints

- Work in the current repository checkout and branch; do not create a worktree or use a subagent.
- Research object is exactly `baseline_20260826` with registry SHA `fc22ca5a973f77faf528cdb3efba4900163e18fe0c08cf79234d79ef22f5c822`.
- Factor identities, normalization, thresholds `0.175/0.025`, state machine, next-open execution, fee `0.0005`, and initial cash `1_000_000` remain frozen.
- ER lookback is exactly 60 trading days; the threshold is the 2020—2025 valid-ER median and never uses performance.
- Weight multipliers are exactly `(0.50, 0.75, 1.00, 1.25, 1.50)`, yielding exactly 625 candidates.
- No 2026 data may be loaded unless a research winner has been written and hashed.
- PASS requires locked-2026 strict improvement in maximum drawdown, Calmar, and win/loss ratio plus trade-support and audit gates; return and Sharpe are report-only.
- Use `apply_patch` for source, test, protocol, and document edits; use only focused tests and full archive validation.

---

### Task 1: Pure ER, weight, and risk-quality primitives

**Files:**
- Create: `src/czsc_trader/regime_weight.py`
- Create: `tests/test_regime_weight.py`

**Interfaces:**
- Produces: `lagged_efficiency_ratio(close: pd.Series, lookback: int = 60) -> pd.Series`
- Produces: `fit_regime_threshold(er: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> float`
- Produces: `classify_regimes(er: pd.Series, threshold: float) -> pd.Series`
- Produces: `project_group_weights(base_weights: pd.Series, groups: Mapping[str, Sequence[str]], trend_multiplier: float, volume_multiplier: float) -> pd.Series`
- Produces: `score_with_regime_weights(factors: pd.DataFrame, regimes: pd.Series, weights: Mapping[str, pd.Series], fallback: pd.Series) -> pd.Series`
- Produces: `closed_trade_ledger(orders: pd.DataFrame, size_rtol: float = 1e-8) -> pd.DataFrame`
- Produces: `risk_quality_metrics(equity: pd.Series, orders: pd.DataFrame, init_cash: float) -> dict[str, float | int | bool]`
- Produces: `strict_quality_pass(baseline: Mapping[str, object], challenger: Mapping[str, object], minimum_closed_trades: int, tolerance: float = 1e-12) -> bool`

- [ ] **Step 1: Write failing tests for strict-lag ER and frozen labels**

```python
def test_er_uses_only_previous_closes_and_frozen_threshold():
    close = pd.Series(range(1, 9), index=pd.date_range("2025-01-01", periods=8))
    er = lagged_efficiency_ratio(close, lookback=3)
    assert er.iloc[:4].isna().all()
    assert er.iloc[4] == pytest.approx(1.0)
    changed = close.copy()
    changed.iloc[4:] = 10_000
    assert lagged_efficiency_ratio(changed, 3).iloc[4] == er.iloc[4]
    labels = classify_regimes(pd.Series([0.2, 0.3, np.nan]), 0.3)
    assert labels.tolist() == ["range", "trend", "warmup"]
```

- [ ] **Step 2: Run the ER test and verify it fails because the module is absent**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_regime_weight.py -k er`

Expected: FAIL with `ModuleNotFoundError` or missing function import.

- [ ] **Step 3: Implement lagged ER, median fitting, and labels**

```python
def lagged_efficiency_ratio(close: pd.Series, lookback: int = 60) -> pd.Series:
    if lookback < 2:
        raise ValueError("ER lookback must be at least two")
    known = close.astype(float).shift(1)
    net = known.sub(known.shift(lookback)).abs()
    path = known.diff().abs().rolling(lookback, min_periods=lookback).sum()
    return net.div(path.where(path.gt(0.0))).rename("er60")

def fit_regime_threshold(er, start, end):
    sample = er.loc[pd.Timestamp(start):pd.Timestamp(end)].dropna()
    if sample.empty:
        raise ValueError("regime calibration has no valid ER observations")
    value = float(sample.median())
    if not np.isfinite(value):
        raise ValueError("regime threshold must be finite")
    return value

def classify_regimes(er, threshold):
    if not np.isfinite(float(threshold)):
        raise ValueError("regime threshold must be finite")
    labels = pd.Series("warmup", index=er.index, dtype="string", name="regime")
    labels.loc[er.notna() & er.lt(float(threshold))] = "range"
    labels.loc[er.notna() & er.ge(float(threshold))] = "trend"
    return labels
```

- [ ] **Step 4: Write failing tests for group projection and regime scoring**

```python
def test_group_projection_preserves_identity_sign_and_l1():
    projected = project_group_weights(BASE, GROUPS, 1.5, 0.5)
    assert list(projected.index) == list(BASE.index)
    assert projected.abs().sum() == pytest.approx(1.0)
    assert np.sign(projected).equals(np.sign(BASE))
    assert projected["trend_a"] / projected["trend_b"] == pytest.approx(BASE["trend_a"] / BASE["trend_b"])

def test_all_one_multipliers_replay_baseline_score():
    same = project_group_weights(BASE, GROUPS, 1.0, 1.0)
    scores = score_with_regime_weights(FACTORS, REGIMES, {"trend": same, "range": same}, same)
    assert_series_equal(scores, score_four_layer(FACTORS, BASE))
```

- [ ] **Step 5: Implement deterministic projection and row-wise regime scoring**

Project by multiplying each frozen group slice, reject missing or duplicate group membership, divide by absolute sum, and call `score_four_layer` separately for `trend`, `range`, and `warmup` masks so the existing floating-point grouping behavior is retained.

- [ ] **Step 6: Write failing tests for closed-trade accounting and metric guards**

```python
def test_closed_trade_ledger_ignores_open_tail_and_deducts_fees():
    ledger = closed_trade_ledger(ORDERS_WITH_ONE_CLOSED_AND_ONE_OPEN)
    assert len(ledger) == 1
    expected = (100 * 12 - 1.2) / (100 * 10 + 1.0) - 1
    assert ledger.iloc[0]["net_return"] == pytest.approx(expected)

def test_quality_pass_requires_three_strict_improvements_and_support():
    assert strict_quality_pass(BASE_METRICS, BETTER, minimum_closed_trades=5)
    assert not strict_quality_pass(BASE_METRICS, {**BETTER, "calmar": BASE_METRICS["calmar"]}, 5)
    assert not strict_quality_pass(BASE_METRICS, {**BETTER, "closed_trade_count": 4}, 5)
```

- [ ] **Step 7: Implement ledger, Calmar, win/loss ratio, and strict gate**

Pair alternating `Buy`/`Sell` orders, reject a leading sell, consecutive same-side order, material size mismatch, or non-finite values. Compute annualized return with exponent `252 / len(equity)`, Calmar against absolute maximum drawdown derived from equity, positive/negative trade means, validity flags, and closed-trade support. The strict gate checks finite metrics, both win and loss support, challenger trade count at least `max(minimum_closed_trades, ceil(0.5 * baseline_count))`, and all three strict improvements beyond tolerance.

- [ ] **Step 8: Run the pure-module tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_regime_weight.py`

Expected: PASS.

- [ ] **Step 9: Commit the pure primitives**

```powershell
git add src/czsc_trader/regime_weight.py tests/test_regime_weight.py
git commit -m "feat: add causal regime weight primitives"
```

---

### Task 2: Deterministic EX19 runner and CLI handler

**Files:**
- Create: `src/czsc_trader/regime_weight_runner.py`
- Modify: `src/czsc_trader/research/handlers.py`
- Modify: `tests/test_regime_weight.py`
- Modify: `tests/test_cli_contracts.py`

**Interfaces:**
- Consumes: all Task 1 pure functions and existing `resolve_baseline`, `generate_factor_frame`, `normalized_signal_factors`, `positions_from_scores`, `run_backtest`, and archive helpers.
- Produces: `validate_regime_weight_protocol(protocol: Mapping[str, object]) -> None`
- Produces: `candidate_grid(multipliers: Sequence[float]) -> tuple[tuple[float, float, float, float], ...]`
- Produces: `run_regime_weight_experiment(raw_dir: Path, baseline_root: Path, experiment_dir: Path, *, execution_commit: str) -> dict[str, object]`
- Registers handler ID: `regime_conditioned_weight_challenge`.

- [ ] **Step 1: Write failing protocol and grid tests**

```python
def test_protocol_freezes_research_test_and_grid_boundaries():
    validate_regime_weight_protocol(VALID_PROTOCOL)
    for key, value in [("er_lookback", 59), ("research_end", "2026-01-01"), ("test_end", "2026-08-29")]:
        changed = deepcopy(VALID_PROTOCOL)
        changed[key] = value
        with pytest.raises(ValueError):
            validate_regime_weight_protocol(changed)

def test_grid_has_625_stable_unique_candidates_and_baseline():
    grid = candidate_grid((0.5, 0.75, 1.0, 1.25, 1.5))
    assert len(grid) == len(set(grid)) == 625
    assert (1.0, 1.0, 1.0, 1.0) in grid
```

- [ ] **Step 2: Run the focused tests and verify missing runner failures**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_regime_weight.py -k "protocol or grid"`

Expected: FAIL on missing imports.

- [ ] **Step 3: Implement strict protocol validation and Cartesian grid**

Validate exact experiment ID, handler, symbol, baseline identity, research/test dates, ER definition, multipliers, thresholds, fee, cash, support counts, tolerances, and `access_2026_after_freeze_only=true`. Generate the grid with `itertools.product` in protocol order and assign deterministic candidate numbers starting at zero.

- [ ] **Step 4: Write failing tests for research selection and 2026 blocking**

Inject a small fake evaluator into an internal `_select_research_winner` helper. Assert that continuous triple improvement plus 3/5 annual drawdown-Calmar wins selects the maximin candidate, a no-winner result has `status=FAIL`, and the market-data loader is never called with a post-2025 cutoff before `frozen_challenger.json` exists.

- [ ] **Step 5: Implement the runner research phase**

Load market data with cutoff `2025-12-31`; resolve and hash-check the baseline; generate frozen factors and baseline target; calculate ER60 and its research median; evaluate all 625 continuous 2021—2025 paths. Only triple-improvement and trade-supported candidates receive five annual backtests. Write `regime_support.csv`, `candidate_results.csv`, `annual_metrics.csv`, `baseline_metrics.json`, and either `candidate_funnel.json` or a fully self-contained `frozen_challenger.json`.

- [ ] **Step 6: Implement freezing, locked test, audits, and archive output**

Hash the winner before the first post-2025 load. Rebuild the winner from the frozen payload; load through `2026-08-28`; evaluate the continuous 2026 window and explanatory Q1/H1/M1-M8 windows; write `test_metrics.json`, `period_comparison.csv`, `orders.csv`, `closed_trades.csv`, `regime_weight_path.csv`, `causal_replay_audit.json`, and `identity_audit.json`. Generate `03_execution.md`, `04_conclusion.md`, and `experiment_manifest.json`; return status PASS/FAIL/ERROR without changing the baseline registry.

- [ ] **Step 7: Register the handler and add a CLI contract test**

```python
def _regime_conditioned_weight(context, experiment_dir):
    from czsc_trader.regime_weight_runner import run_regime_weight_experiment
    return run_regime_weight_experiment(
        context.raw_dir,
        context.baseline_root,
        experiment_dir,
        execution_commit=_git_head(context.root),
    )
```

Append `FunctionHandler("regime_conditioned_weight_challenge", _regime_conditioned_weight)` and assert the standard experiment CLI delegates exactly once for a matching temporary protocol.

- [ ] **Step 8: Run runner and CLI tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_regime_weight.py tests/test_cli_contracts.py`

Expected: PASS.

- [ ] **Step 9: Commit the runner**

```powershell
git add src/czsc_trader/regime_weight_runner.py src/czsc_trader/research/handlers.py tests/test_regime_weight.py tests/test_cli_contracts.py
git commit -m "feat: add regime-conditioned weight experiment"
```

---

### Task 3: Preregister 0901_EX19

**Files:**
- Create: `experiments/0901_EX19/01_goal.md`
- Create: `experiments/0901_EX19/02_design.md`
- Create: `experiments/0901_EX19/artifacts/protocol.json`

**Interfaces:**
- Consumes: exact protocol validator from Task 2.
- Produces: immutable pre-performance research contract for the CLI runner.

- [ ] **Step 1: Write the goal and PASS contract**

State that a frozen candidate may sacrifice return, but must strictly improve locked-2026 maximum drawdown, Calmar, and average-win/average-loss ratio with the agreed trade-support and audit gates. State that 2026 is a locked test already seen by prior research, not a newly unseen sample.

- [ ] **Step 2: Write the concise experiment design**

Copy the exact ER formula, median calibration, 625 group multiplier combinations, research selection ranking, freeze-before-test rule, metrics, invalid-value behavior, and no-rescue boundaries from the spec.

- [ ] **Step 3: Write the machine-readable protocol**

The JSON includes exact baseline SHA, dates, lookback, label names, multiplier list, group reference, thresholds, fee, cash, research/test trade support, annual win count, tolerance, `access_2026_after_freeze_only`, report-only metrics, and `status: PRE_REGISTERED`.

- [ ] **Step 4: Validate the protocol through focused tests and inspect the staged diff**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_regime_weight.py -k protocol`

Run: `git diff --check`

Expected: tests PASS and no whitespace errors.

- [ ] **Step 5: Commit preregistration before performance access**

```powershell
git add experiments/0901_EX19/01_goal.md experiments/0901_EX19/02_design.md experiments/0901_EX19/artifacts/protocol.json
git commit -m "research: preregister regime-conditioned weight challenge"
```

---

### Task 4: Execute, validate, and archive the experiment

**Files:**
- Generate: `experiments/0901_EX19/03_execution.md`
- Generate: `experiments/0901_EX19/04_conclusion.md`
- Generate: `experiments/0901_EX19/experiment_manifest.json`
- Generate: declared files under `experiments/0901_EX19/artifacts/`

**Interfaces:**
- Consumes: preregistered EX19 directory and installed unified CLI.
- Produces: immutable PASS/FAIL/ERROR archive and, only after a research winner, locked-2026 evidence.

- [ ] **Step 1: Record the clean preregistration commit and run EX19 once**

Run: `.\.venv\Scripts\czsc-trader.exe experiment run --dir experiments/0901_EX19 --repo-root . --format json`

Expected: one JSON result; runner status may be PASS or FAIL, but no unhandled exception.

- [ ] **Step 2: Inspect machine-readable selection and access boundaries**

If no research winner exists, verify there is no frozen file, no test metrics, and `access_2026=false`. If a winner exists, verify its hash predates the locked-test artifacts and the identity audit confirms exact replay.

- [ ] **Step 3: Validate the new archive and all existing archives**

Run: `.\.venv\Scripts\czsc-trader.exe archive validate --archive experiments/0901_EX19 --repo-root . --format json`

Run: `.\.venv\Scripts\czsc-trader.exe archive validate --all --repo-root . --format json`

Expected: PASS with 44 validated archives.

- [ ] **Step 4: Run the focused regression suite**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_regime_weight.py tests/test_cli_contracts.py tests/test_cli_e2e.py`

Expected: PASS.

- [ ] **Step 5: Commit the immutable experiment archive**

```powershell
git add experiments/0901_EX19
git commit -m "research: archive regime-conditioned weight challenge"
```

---

### Task 5: Handoff update and final verification

**Files:**
- Modify: `README.md`
- Modify: `docs/RESEARCH_HANDOFF.md`

**Interfaces:**
- Consumes: validated EX19 conclusion and manifest.
- Produces: current cross-machine state without references to local outputs, runtime caches, or virtual environments as evidence.

- [ ] **Step 1: Update only verified research facts**

Add EX19 to the progress table, update the archive count to 44, state the exact research and locked-test result, preserve `baseline_20260826` unless the user later explicitly authorizes promotion, and link the four EX19 research documents in the handoff prompt.

- [ ] **Step 2: Run final evidence checks**

Run: `git diff --check`

Run: `.\.venv\Scripts\czsc-trader.exe archive validate --all --repo-root . --format json`

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_regime_weight.py tests/test_cli_contracts.py tests/test_cli_e2e.py`

Expected: 44 archives PASS and all focused tests PASS.

- [ ] **Step 3: Commit documentation and report without merging**

```powershell
git add README.md docs/RESEARCH_HANDOFF.md
git commit -m "docs: hand off regime-conditioned weight result"
```

Report the branch, commits, research winner identity or failure reason, 2026 access status, baseline/challenger metrics, audit status, and focused verification. Keep the branch unmerged and unpushed until explicitly requested.
