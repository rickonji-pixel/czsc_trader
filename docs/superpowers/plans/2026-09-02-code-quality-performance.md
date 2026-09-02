# Code Quality and Runtime Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify comparison metrics, reduce routine CLI and test startup cost, and establish a small static-quality gate without changing strategy behavior.

**Architecture:** Centralize equity-derived Sharpe calculation in `strategy_metrics`, move pure Markdown rendering into `reporting`, and defer application-service imports until a CLI command is selected. Reclassify existing tests so the default suite keeps one installed-entry-point backtest while repository-wide archive validation remains explicit.

**Tech Stack:** Python 3.12, pandas, NumPy, vectorbt, pytest, Ruff, argparse

**Spec:** `docs/superpowers/specs/2026-09-02-code-quality-performance-design.md`

## Global Constraints

- Preserve `baseline_20260901`, `execution_policy_20260902`, target positions, orders, equity, returns, maximum drawdown, Calmar ratio, and win/loss ratio.
- Use 252 sessions, zero risk-free rate, and sample standard deviation for every comparison Sharpe ratio.
- Do not change raw data, experiment artifacts, baseline identity, execution-policy identity, or LF/raw-byte hashing semantics.
- Keep the public CLI resource names, arguments, exit codes, and JSON shapes unchanged.
- Add no runtime dependency; Ruff belongs only to the `test` optional dependency.

---

### Task 1: Unify comparison Sharpe calculation

**Files:**
- Create: `tests/test_strategy_metrics.py`
- Modify: `src/czsc_trader/strategy_metrics.py`
- Modify: `src/czsc_trader/execution_policy.py`
- Modify: `src/czsc_trader/backtest_runner.py`

**Interfaces:**
- Produces: `annualized_sharpe(equity: pd.Series, init_cash: float, *, annualization: float = 252.0) -> float | None`
- Changes: `strategy_comparison_metrics(equity, orders, init_cash)` derives Sharpe internally.
- Consumes: independently funded equity series and initial cash.

- [ ] **Step 1: Write failing metric tests**

```python
def test_annualized_sharpe_includes_first_session_return() -> None:
    equity = pd.Series([102.0, 101.0, 104.0])
    returns = pd.Series([0.02, 101.0 / 102.0 - 1.0, 104.0 / 101.0 - 1.0])
    expected = np.sqrt(252.0) * returns.mean() / returns.std(ddof=1)
    assert annualized_sharpe(equity, 100.0) == pytest.approx(expected)


def test_annualized_sharpe_rejects_degenerate_series() -> None:
    assert annualized_sharpe(pd.Series([100.0]), 100.0) is None
    assert annualized_sharpe(pd.Series([100.0, 100.0]), 100.0) is None
```

- [ ] **Step 2: Run the focused tests and observe the missing helper failure**

Run: `python -m pytest tests/test_strategy_metrics.py -q`

Expected: collection fails because `annualized_sharpe` does not exist.

- [ ] **Step 3: Implement the centralized formula and remove injected Sharpe**

```python
def annualized_sharpe(
    equity: pd.Series,
    init_cash: float,
    *,
    annualization: float = 252.0,
) -> float | None:
    values = equity.astype(float)
    if values.empty or len(values) < 2:
        return None
    prior = values.shift(1)
    prior.iloc[0] = float(init_cash)
    returns = values.div(prior).sub(1.0)
    volatility = float(returns.std(ddof=1))
    if not np.isfinite(volatility) or volatility <= 0.0:
        return None
    result = np.sqrt(float(annualization)) * float(returns.mean()) / volatility
    return _finite_or_none(float(result))
```

Update all four comparison paths to call `strategy_comparison_metrics` without
a Sharpe argument. Delete the duplicate formula from `policy_metrics`.

- [ ] **Step 4: Run focused metric and execution-policy tests**

Run: `python -m pytest tests/test_strategy_metrics.py tests/test_cli_e2e.py -q`

Expected: the metric tests pass; the existing end-to-end expected Sharpe values fail until Task 2 updates the versioned report contract.

- [ ] **Step 5: Commit the metric kernel**

```powershell
git add src/czsc_trader/strategy_metrics.py src/czsc_trader/execution_policy.py src/czsc_trader/backtest_runner.py tests/test_strategy_metrics.py
git commit -m "fix: unify comparison Sharpe calculation"
```

### Task 2: Extract and version backtest reporting

**Files:**
- Create: `src/czsc_trader/reporting/backtest_report.py`
- Create: `tests/test_backtest_report.py`
- Modify: `src/czsc_trader/backtest_runner.py`
- Modify: `tests/test_cli_e2e.py`

**Interfaces:**
- Produces: `render_backtest_report(symbol: str, baseline_version: str, metrics: dict[str, object], chart_files: list[str]) -> str`
- Consumes: already calculated metrics; performs no filesystem access.

- [ ] **Step 1: Write a failing pure-renderer test**

```python
def test_render_backtest_report_formats_all_comparison_rows() -> None:
    text = render_backtest_report(
        "588080.SH",
        "baseline_test",
        {"windows": {"full": {"start": "2026-01-01", "end": "2026-01-02", "strategies": STRATEGIES}}},
        ["chart.html", "ma_chart.html"],
    )
    assert "| 活动基线·次日开盘 |" in text
    assert "| 活动基线·执行规则 |" in text
    assert "| BuyHold |" in text
    assert "| MA5/MA20 |" in text
```

- [ ] **Step 2: Run the renderer test and observe the missing module failure**

Run: `python -m pytest tests/test_backtest_report.py -q`

Expected: collection fails because `reporting.backtest_report` does not exist.

- [ ] **Step 3: Move the pure renderer and update manifest schema**

Move `_report` without behavioral changes, rename it
`render_backtest_report`, import it in `backtest_runner.py`, and set:

```python
"metrics_schema_version": 4,
```

Update end-to-end expected Sharpe values to values produced by the unified
formula. Assert the manifest schema is 4 and preserve unavailable win/loss
reasons through `win_loss_ratio_status`.

- [ ] **Step 4: Run report and end-to-end tests**

Run: `python -m pytest tests/test_strategy_metrics.py tests/test_backtest_report.py tests/test_cli_e2e.py -q`

Expected: all pass and generated equity/order assertions remain unchanged.

- [ ] **Step 5: Commit report extraction**

```powershell
git add src/czsc_trader/reporting/backtest_report.py src/czsc_trader/backtest_runner.py tests/test_backtest_report.py tests/test_cli_e2e.py
git commit -m "refactor: isolate versioned backtest reporting"
```

### Task 3: Lazy-load CLI command services

**Files:**
- Create: `tests/test_cli_imports.py`
- Modify: `src/czsc_trader/cli/main.py`
- Modify: `src/czsc_trader/application/data_service.py`

**Interfaces:**
- Preserves: `build_parser()`, `main(argv)`, all command handler names and command result shapes.
- Changes: service imports occur inside the matching handler.

- [ ] **Step 1: Write a failing import-isolation test**

```python
def test_cli_parser_does_not_import_backtest_stack() -> None:
    code = "import sys; import czsc_trader.cli.main; print('vectorbt' in sys.modules)"
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert completed.stdout.strip() == "False"
```

- [ ] **Step 2: Run the import test and observe eager loading**

Run: `python -m pytest tests/test_cli_imports.py -q`

Expected: FAIL because `vectorbt` is loaded when `cli.main` is imported.

- [ ] **Step 3: Move service imports into command handlers**

Use local imports such as:

```python
def _backtest_run(args: argparse.Namespace):
    from czsc_trader.application.backtest_service import BacktestCommand, run_backtest

    return run_backtest(_context(args), BacktestCommand(...))
```

Move `prepare_market_data` from module scope into `prepare_data`. Keep local
data loading available to `validate_data`.

- [ ] **Step 4: Verify import isolation and CLI behavior**

Run: `python -m pytest tests/test_cli_imports.py tests/test_cli_e2e.py -q`

Expected: both pass; a separate timing probe reports `cli.main` import below 1.0 second.

- [ ] **Step 5: Commit lazy loading**

```powershell
git add src/czsc_trader/cli/main.py src/czsc_trader/application/data_service.py tests/test_cli_imports.py
git commit -m "perf: lazy-load CLI command services"
```

### Task 4: Reclassify and accelerate tests

**Files:**
- Create: `tests/test_execution_policy.py`
- Create: `tests/test_identity_and_archives.py`
- Create: `tests/test_robustness.py`
- Create: `tests/test_repository_contract.py`
- Modify: `tests/test_cli_e2e.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Preserves: all existing assertions unless explicitly redundant with the one installed CLI backtest.
- Produces: `archive` pytest marker for the complete frozen-evidence audit.

- [ ] **Step 1: Move unit tests without changing assertions**

Move identity/archive fixture tests, robustness tests, execution-policy/advice
unit tests, and repository contracts to the files listed above. Keep shared
`REPO_ROOT` and tracked-symbol constants local to the files that use them.

- [ ] **Step 2: Replace repeated CLI subprocess checks with in-process checks**

Validate the four symbols in one test:

```python
def test_all_tracked_market_data_validates() -> None:
    context = RepositoryContext.discover(REPO_ROOT)
    for symbol in TRACKED_SYMBOLS:
        result = validate_data(context, symbol).result
        assert result["symbol"] == symbol
        assert result["requested_end"] == "2026-09-01"
```

Call `show_baseline` directly for the active-candidate contract and call
`build_parser().format_help()` for the resource surface. Remove the installed
daily-advice subprocess test; retain direct advice mapping coverage.

- [ ] **Step 3: Mark the full archive audit and configure default exclusion**

```python
@pytest.mark.archive
def test_all_frozen_experiment_archives_validate() -> None:
    context = RepositoryContext.discover(REPO_ROOT)
    result = validate_archives(context, all_archives=True).result
    assert result["validated_count"] == 48
```

Configure:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "--strict-markers --tb=short -m 'not archive'"
markers = ["archive: validates every immutable experiment archive"]
```

- [ ] **Step 4: Run fast and archive suites with durations**

Run: `python -m pytest -q --durations=15`

Expected: default suite passes in less than 20 seconds.

Run: `python -m pytest -q -m archive`

Expected: all 48 frozen archives validate.

- [ ] **Step 5: Commit the lean test architecture**

```powershell
git add tests pyproject.toml
git commit -m "test: separate fast checks from archive audit"
```

### Task 5: Add static checks and complete verification

**Files:**
- Modify: `pyproject.toml`
- Modify: `README.md`

**Interfaces:**
- Adds: Ruff only to `project.optional-dependencies.test`.
- Documents: fast suite, archive audit, and static check commands.

- [ ] **Step 1: Add Ruff configuration**

```toml
[project.optional-dependencies]
test = ["pytest>=8.0", "ruff>=0.12"]

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["E4", "E7", "E9", "F"]
```

- [ ] **Step 2: Install the updated editable test environment**

Run: `python -m pip install -e ".[test]"`

Expected: installation succeeds and `python -m ruff --version` prints a version.

- [ ] **Step 3: Run Ruff and fix only reported correctness/style errors**

Run: `python -m ruff check src tests`

Expected: no findings. Do not run a repository-wide formatter.

- [ ] **Step 4: Document verification commands**

Add a concise README section containing:

```powershell
python -m pytest -q
python -m pytest -q -m archive
python -m ruff check src tests
```

- [ ] **Step 5: Run final verification**

Run:

```powershell
python -m pytest -q --durations=15
python -m pytest -q -m archive
python -m ruff check src tests
python -m pip check
python -m compileall -q src
git diff --check
git status --short
```

Expected: every command passes; default tests finish under 20 seconds; status
contains only intended source, test, configuration, documentation, and plan
changes.

- [ ] **Step 6: Commit quality tooling and documentation**

```powershell
git add pyproject.toml README.md
git commit -m "chore: add lean quality verification"
```
