# MA5/MA20 Backtest Comparison Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a causal MA5/MA20 strategy to every backtest, compare it with the active baseline and BuyHold using five common metrics, and publish a separate interactive MA daily chart.

**Architecture:** Generate one full-history MA target before slicing evaluation windows, then reuse the existing next-open execution engine for all three strategies. A generic metric module normalizes return, drawdown, Calmar, win/loss ratio, and Sharpe; the runner publishes one nested three-strategy result schema, CSV artifacts, Markdown tables, and separate baseline/MA charts.

**Tech Stack:** Python 3.12, pandas, NumPy, vectorbt, Plotly, pytest, argparse CLI.

**Spec:** `docs/MA_COMPARISON_DESIGN.md`

## Global Constraints

- Work only on `codex/0901-ma-comparison`; do not use a Git worktree.
- Preserve the activity baseline rule and its existing chart and audit behavior.
- MA5/MA20 uses daily close, full/cash positions, and next-session-open execution.
- All strategies share dates, initial cash, and fee rate.
- Undefined metrics serialize as JSON null and render as `N/A`.
- Keep exactly one test file, `tests/test_cli_e2e.py`, and do not add network access.
- Use the fixed 2026-01-05 through 2026-08-21 run for literal regression values.

---

### Task 1: Establish the failing public contract

**Files:**
- Modify: `tests/test_cli_e2e.py`

**Interfaces:**
- Consumes: installed `czsc-trader backtest run` output.
- Produces: a failing end-to-end contract for nested metrics, MA causality, report rows, CSVs, and HTML.

- [ ] **Step 1: Replace flat comparison expectations**

Define the literal metric key set:

```python
STRATEGY_METRIC_KEYS = {
    "max_drawdown",
    "calmar",
    "win_loss_ratio",
    "return",
    "sharpe",
}
```

For the existing fixed backtest, assert `windows.full` contains `start`, `end`,
and exactly `active_baseline`, `buyhold`, `ma5_ma20`. Assert each strategy has
the five keys. Keep the active-baseline return literal
`0.7528525916956634` and add independently calculated MA literals:

```python
assert ma["return"] == pytest.approx(0.20069278199087237)
assert ma["max_drawdown"] == pytest.approx(-0.22945460734778733)
assert ma["calmar"] == pytest.approx(1.520558369439513)
assert ma["win_loss_ratio"] == pytest.approx(3.2767930702460384)
assert ma["sharpe"] == pytest.approx(1.234024171393839)
assert buyhold["win_loss_ratio"] is None
```

- [ ] **Step 2: Assert causal MA artifacts**

Load `ma_signals.csv` and `ma_orders.csv`. Assert the signal contains
`dt,close,ma5,ma20,target_position`, the first-window target on 2026-01-05 is
`1.0`, the first order signal is 2025-12-31, and it executes on 2026-01-05.
For every order, assert execution date is the next available trading date after
signal date in the tracked daily data.

- [ ] **Step 3: Assert report and chart artifacts**

Assert `report.md` contains one `## full` subsection and exactly these strategy
row prefixes:

```python
"| 活动基线 |"
"| BuyHold |"
"| MA5/MA20 |"
```

Assert `ma_chart.html` exists and its text contains `MA5`, `MA20`, `MA买入`,
`MA卖出`, and Plotly's unified-hover setting. Keep assertions for the existing
baseline chart, audit, and report.

- [ ] **Step 4: Run the focused test and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_cli_e2e.py::test_installed_cli_runs_audited_backtest -q
```

Expected: FAIL because the current result is flat and no MA artifacts exist.

### Task 2: Implement causal MA targets and generic metrics

**Files:**
- Create: `src/czsc_trader/moving_average.py`
- Create: `src/czsc_trader/strategy_metrics.py`
- Modify: `src/czsc_trader/regime_weight.py`

**Interfaces:**
- Produces: `moving_average_signals(daily: pd.DataFrame, fast: int = 5, slow: int = 20) -> pd.DataFrame`.
- Produces: `strategy_comparison_metrics(equity: pd.Series, orders: pd.DataFrame, init_cash: float, sharpe: float) -> dict[str, float | None]`.
- Produces: `closed_trade_ledger(orders: pd.DataFrame) -> pd.DataFrame` inside the generic metric module.

- [ ] **Step 1: Implement MA signal construction**

Normalize `daily.dt` to a sorted DatetimeIndex, validate positive windows with
`fast < slow`, calculate rolling simple averages, and return columns
`close,ma5,ma20,target_position`. Use `(ma5 > ma20).astype(float)`, which keeps
warmup at zero because NaN comparisons are false.

- [ ] **Step 2: Implement common five metrics**

Move the generic closed-trade pairing logic from `regime_weight.py` into
`strategy_metrics.py`. Calculate marked-to-market return, annualized return,
maximum drawdown, Calmar, closed-trade win/loss ratio, and accept the engine's
Sharpe. Normalize non-finite optional ratios to `None` before JSON serialization.

- [ ] **Step 3: Remove retired metric helpers from regime module**

Delete the now-unused `closed_trade_ledger`, `risk_quality_metrics`,
`strict_quality_pass`, and `candidate_relative_improvements` functions from
`regime_weight.py`. Keep only active regime classification and scoring behavior.

- [ ] **Step 4: Compile the new modules**

```powershell
.\.venv\Scripts\python.exe -m compileall -q src\czsc_trader
```

Expected: exit code 0.

### Task 3: Orchestrate three independently funded strategies

**Files:**
- Modify: `src/czsc_trader/backtest_runner.py`

**Interfaces:**
- Consumes: MA signal frame, existing active-baseline results, existing `run_backtest` and `run_period_backtests`.
- Produces: nested `windows.<name>.strategies` metrics and MA CSV artifacts.

- [ ] **Step 1: Generate MA signals before window slicing**

After causal market truncation, call `moving_average_signals(causal_data.daily)`.
Run `run_period_backtests` with `ma_signals.target_position`, the same periods,
fee, and initial cash, without factor provenance.

- [ ] **Step 2: Run BuyHold through the shared engine**

For each baseline result's actual start/end, create a target series of `1.0` and
call `run_backtest` with `initial_target=1.0`. Do not synthesize a closing sell.

- [ ] **Step 3: Build the nested strategy schema**

For each window publish:

```python
{
    "start": baseline_result.metrics["start"],
    "end": baseline_result.metrics["end"],
    "strategies": {
        "active_baseline": strategy_comparison_metrics(...),
        "buyhold": strategy_comparison_metrics(...),
        "ma5_ma20": strategy_comparison_metrics(...),
    },
}
```

Remove the old return/sharpe/drawdown difference fields and `_comparison_metrics`.

- [ ] **Step 4: Write MA CSV artifacts**

Write one full causal `ma_signals.csv`; for each window write
`ma_orders<suffix>.csv` and `ma_equity<suffix>.csv`. MA equity columns are
`dt,equity,target_position,execution_position,ma5,ma20`.

### Task 4: Publish report and MA chart

**Files:**
- Create: `src/czsc_trader/ma_charting.py`
- Modify: `src/czsc_trader/backtest_runner.py`

**Interfaces:**
- Produces: `write_ma_period_chart(daily, signals, orders, start, end, title, output_path) -> Path`.
- Produces: one six-column Markdown comparison table per window.

- [ ] **Step 1: Implement the single-panel MA chart**

Build a Plotly figure with daily candlesticks, MA5, MA20, and Buy/Sell markers
from order execution dates. Set `hovermode="x unified"`, hide the range slider,
and add explicit range breaks for every missing calendar date between the first
and last period session.

- [ ] **Step 2: Write one MA chart per window**

Use `ma_chart<suffix>.html`, keep the existing `chart<suffix>.html`, and append
both names to the manifest chart list and report link list.

- [ ] **Step 3: Replace report comparison layout**

For each window emit `## <window>` followed by the fixed table header and rows in
this order: 活动基线, BuyHold, MA5/MA20. Format drawdown and return as percentages,
ratios to three decimals, and `None` as `N/A`.

- [ ] **Step 4: Extend manifest metadata**

Record metric schema version 2 and MA parameters `{fast: 5, slow: 20,
execution: next_session_open, position: full_or_cash}`. Preserve baseline identity,
data hashes, periods, and audit status.

- [ ] **Step 5: Run the focused test and verify GREEN**

Run the Task 1 test command.

Expected: PASS with all literal MA metrics and artifacts.

### Task 5: Document and verify the delivered chain

**Files:**
- Modify: `README.md`
- Modify: `docs/RESEARCH_HANDOFF.md`
- Retain: `docs/MA_COMPARISON_DESIGN.md`
- Delete: `docs/MA_COMPARISON_PLAN.md`

**Interfaces:**
- Produces: current user and cross-machine instructions without local output assumptions.

- [ ] **Step 1: Update current documentation**

Document that every backtest now compares active baseline, BuyHold, and MA5/MA20;
define the five metrics, `N/A` behavior, MA execution timing, and MA chart artifact.
Do not add local output paths or dated backtest results to the handoff document.

- [ ] **Step 2: Remove the temporary implementation plan**

Delete `docs/MA_COMPARISON_PLAN.md` after implementation; the committed design
remains the durable architecture reference and Git history retains the plan.

- [ ] **Step 3: Run complete minimal verification**

```powershell
.\.venv\Scripts\python.exe -m compileall -q src packages\dataflows\src
.\.venv\Scripts\python.exe -m pytest tests\test_cli_e2e.py -q
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\czsc-trader.exe baseline validate --version baseline_20260901 --symbol 588080.SH
.\.venv\Scripts\czsc-trader.exe archive validate --all
```

Expected: compile exit 0, all tests pass, no broken dependencies, baseline PASS,
and 46 archives PASS.

- [ ] **Step 4: Run a three-window acceptance backtest**

Run 588080.SH with `configs/backtest_windows/2026.json` for `2026Q1`, `2026H1`,
and `2026FULL`. Inspect each report and both charts. Confirm all metrics are finite
or null under the registered rules and every output audit is PASS.

- [ ] **Step 5: Audit repository boundaries**

Run `git diff --check`, confirm only `tests/test_cli_e2e.py` exists under tests,
confirm no file under `experiments/` changed, and verify the active baseline JSON
and registry hashes are unchanged.

- [ ] **Step 6: Commit implementation**

```powershell
git add -A
git commit -m "feat: compare MA5 MA20 in backtests"
```

- [ ] **Step 7: Verify final branch state**

Run `git status --short --branch` and `git log -3 --oneline --decorate`.

Expected: clean feature branch with design and implementation commits.
