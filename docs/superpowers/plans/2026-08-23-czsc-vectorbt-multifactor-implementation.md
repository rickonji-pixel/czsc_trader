# CZSC Vectorbt Multifactor Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible, causally tuned long/cash strategy for 588080.SH whose factors come from CZSC and whose execution and target-period returns are verified with vectorbt.

**Architecture:** Load and reconcile the nine immutable CSV inputs, generate a daily as-of factor matrix from CZSC signals on 30-minute/daily/weekly bars, and select a compact score rule monthly using only prior observations. Shift target positions to the next open, execute them with vectorbt, audit every information and execution timestamp, and export machine-readable evidence plus a Markdown report.

**Tech Stack:** Python 3.12, pandas 3, NumPy 2, CZSC 1.0.1, vectorbt 1.1.0, pytest 8

**Spec:** `docs/superpowers/specs/2026-08-23-czsc-vectorbt-multifactor-design.md`

## Global Constraints

- The only market-data source is `data/raw/*.csv`; research code must not access the network.
- CZSC must be version 1.0.1 and vectorbt must be version 1.1.0.
- Decisions use information available by day `t` 15:00 and execute no earlier than day `t+1` open.
- Trading is daily, long/cash only, without leverage or shorting.
- Fees plus slippage are 0.05% per side; ETF stamp duty is zero.
- Monthly parameter updates use at most 252 completed prior trading days and require at least 120 days.
- Outputs report 2026Q1, 2026H1, and 2026-01 through 2026-08-21 against continuous Buy & Hold.

---

### Task 1: Package Skeleton and Immutable Market Data

**Files:**
- Create: `pyproject.toml`
- Create: `src/czsc_trader/__init__.py`
- Create: `src/czsc_trader/data.py`
- Create: `tests/test_data.py`

**Interfaces:**
- Consumes: the nine CSV files below `data/raw`.
- Produces: `MarketData(intraday, daily, weekly, hashes)` and `load_market_data(raw_dir: Path) -> MarketData`.

- [ ] **Step 1: Write failing data tests**

```python
from pathlib import Path
from czsc_trader.data import load_market_data


def test_loads_verified_market_data():
    data = load_market_data(Path("data/raw"))
    assert len(data.intraday) == 5112
    assert len(data.daily) == 639
    assert len(data.weekly) == 136
    assert data.intraday.groupby(data.intraday["dt"].dt.normalize()).size().eq(8).all()
    assert len(data.hashes) == 9


def test_cross_frequency_prices_reconcile():
    data = load_market_data(Path("data/raw"))
    intraday = data.intraday.assign(date=data.intraday["dt"].dt.normalize())
    agg = intraday.groupby("date").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"),
    )
    daily = data.daily.set_index("dt")
    assert agg.equals(daily[["open", "high", "low", "close"]])
```

- [ ] **Step 2: Run tests and confirm the missing-package failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_data.py -v`

Expected: collection fails because `czsc_trader.data` does not exist.

- [ ] **Step 3: Add packaging metadata and the minimal data loader**

```python
@dataclass(frozen=True)
class MarketData:
    intraday: pd.DataFrame
    daily: pd.DataFrame
    weekly: pd.DataFrame
    hashes: dict[str, str]


def load_market_data(raw_dir: Path) -> MarketData:
    """Read year partitions, normalize to dt/symbol/vol, and fail on bad data."""
```

The loader must require exact filenames, parse `datetime`/`date` as naive timestamps, rename `volume` to `vol`, add `symbol="588080.SH"`, concatenate years, and validate schemas, ordering, duplicates, nulls, positive OHLC, non-negative volume/amount, eight expected intraday timestamps, daily price reconciliation, daily volume/amount relative tolerance below `1e-6`, and exact weekly reconciliation.

- [ ] **Step 4: Install test dependencies and run the data tests**

Run: `.\.venv\Scripts\python.exe -m pip install -e ".[test]"`

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_data.py -v`

Expected: all data tests pass.

- [ ] **Step 5: Commit the data boundary**

```powershell
git add pyproject.toml src/czsc_trader/__init__.py src/czsc_trader/data.py tests/test_data.py
git commit -m "feat: add immutable K-line data boundary"
```

### Task 2: CZSC Daily Factor Matrix

**Files:**
- Create: `src/czsc_trader/factors.py`
- Create: `tests/test_factors.py`

**Interfaces:**
- Consumes: `MarketData` from Task 1.
- Produces: `FactorResult(frame: pd.DataFrame, unknown_values: dict[str, int])` and `generate_factor_frame(data: MarketData) -> FactorResult`.

- [ ] **Step 1: Write failing factor tests**

```python
from czsc_trader.data import load_market_data
from czsc_trader.factors import generate_factor_frame


def test_factors_are_daily_and_bounded():
    data = load_market_data(Path("data/raw"))
    result = generate_factor_frame(data)
    assert result.frame.index.is_unique
    assert result.frame.index.is_monotonic_increasing
    assert {"structure", "trend", "volume_position"}.issubset(result.frame.columns)
    assert result.frame[["structure", "trend", "volume_position"]].abs().le(1).all().all()


def test_future_truncation_does_not_change_past_factors():
    data = load_market_data(Path("data/raw"))
    cutoff = pd.Timestamp("2025-06-30")
    full = generate_factor_frame(data).frame.loc[:cutoff]
    truncated = generate_factor_frame(data.truncate(cutoff)).frame
    pd.testing.assert_frame_equal(full, truncated)
```

- [ ] **Step 2: Run the factor tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_factors.py -v`

Expected: fails because `czsc_trader.factors` and `MarketData.truncate` do not exist.

- [ ] **Step 3: Add causal truncation and CZSC input adapters**

```python
def to_raw_bars(df: pd.DataFrame, freq: str) -> list:
    cols = ["dt", "symbol", "open", "close", "high", "low", "vol", "amount"]
    return czsc.format_standard_kline(df[cols], freq=freq)


def _run_signals(df: pd.DataFrame, freq: str, config: list[dict], init_n: int) -> pd.DataFrame:
    bars = to_raw_bars(df, freq)
    return czsc.generate_czsc_signals(bars, config, sdt=str(df["dt"].min().date()), init_n=init_n, df=True)
```

Use 30-minute configs for `cxt_bi_status_V230101` and `cxt_third_buy_V230228`; daily configs for `cxt_bi_status_V230101`, SMA 5/10/20 `tas_ma_base_V221101`, `tas_macd_base_V221028`, `vol_window_V230731`, and `pressure_support_V240406`; weekly config for `cxt_bi_status_V230101`. Select 15:00 intraday rows and backward-asof merge only already-completed weekly rows.

- [ ] **Step 4: Add explicit categorical mappings and grouped factors**

```python
POSITIVE_TOKENS = ("向上", "多头", "三买", "支撑", "强势", "放量")
NEGATIVE_TOKENS = ("向下", "空头", "压力", "弱势", "缩量")


def signal_value_to_score(value: object) -> int:
    text = str(value)
    if any(token in text for token in POSITIVE_TOKENS):
        return 1
    if any(token in text for token in NEGATIVE_TOKENS):
        return -1
    return 0
```

Average mapped columns within the structure, trend, and volume/position groups, clip each group to `[-1, 1]`, preserve raw signal columns with a `raw__` prefix, and record unrecognized non-neutral values.

- [ ] **Step 5: Run factor tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_factors.py -v`

Expected: all factor tests pass, including truncation invariance.

- [ ] **Step 6: Commit the CZSC factor layer**

```powershell
git add src/czsc_trader/data.py src/czsc_trader/factors.py tests/test_factors.py
git commit -m "feat: generate causal CZSC factor matrix"
```

### Task 3: Monthly Walk-Forward Selector

**Files:**
- Create: `src/czsc_trader/walk_forward.py`
- Create: `tests/test_walk_forward.py`

**Interfaces:**
- Consumes: daily OHLC and grouped factor frame.
- Produces: `WalkForwardResult(target_position, scores, selections)` and `run_walk_forward(daily, factors, fee_rate=0.0005) -> WalkForwardResult`.

- [ ] **Step 1: Write failing causality and state tests**

```python
def test_future_prices_cannot_change_past_selections(sample_daily, sample_factors):
    first = run_walk_forward(sample_daily, sample_factors)
    changed = sample_daily.copy()
    changed.loc[changed.index > "2025-06-30", "close"] *= 3
    second = run_walk_forward(changed, sample_factors)
    pd.testing.assert_frame_equal(
        first.selections.loc[:"2025-06-30"], second.selections.loc[:"2025-06-30"]
    )


def test_positions_are_long_or_cash(sample_daily, sample_factors):
    result = run_walk_forward(sample_daily, sample_factors)
    assert set(result.target_position.dropna().unique()) <= {0.0, 1.0}
```

- [ ] **Step 2: Run tests and verify missing-module failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_walk_forward.py -v`

Expected: fails because `czsc_trader.walk_forward` does not exist.

- [ ] **Step 3: Implement compact predeclared candidates**

```python
@dataclass(frozen=True)
class Rule:
    weights: tuple[float, float, float]
    enter: float
    exit: float
    confirm_days: int
    min_hold_days: int


CANDIDATES = tuple(
    Rule(weights, enter, exit_, confirm, hold)
    for weights in ((0.5, 0.3, 0.2), (0.4, 0.4, 0.2), (0.4, 0.3, 0.3))
    for enter in (0.05, 0.20, 0.35)
    for exit_ in (-0.20, 0.0, 0.10)
    if exit_ < enter
    for confirm in (1, 2)
    for hold in (1, 3, 5)
)
```

Implement a pure `positions_for_rule(factors, rule) -> pd.Series` state machine. It must enforce confirmation and minimum hold without reading prices.

- [ ] **Step 4: Implement prior-only monthly selection**

At each month start, slice observations strictly before that date, retain the latest 252 rows, and use the default rule until 120 rows exist. Score each candidate with prior open-to-open strategy returns after the one-day signal shift and 5 bps per position change:

```python
objective = excess_return - 0.5 * max(0.0, abs(strategy_drawdown) - abs(buyhold_drawdown)) - 0.10 * turnover
```

Tie-break by lower turnover, lower parameter distance from the previous month, then candidate declaration order. Save `as_of_date`, `train_start`, `train_end`, rule fields, objective, and candidate count.

- [ ] **Step 5: Run walk-forward tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_walk_forward.py -v`

Expected: causality, long/cash, frozen-month, and minimum-history tests pass.

- [ ] **Step 6: Commit the selector**

```powershell
git add src/czsc_trader/walk_forward.py tests/test_walk_forward.py
git commit -m "feat: add causal monthly walk-forward selector"
```

### Task 4: Vectorbt Execution and Period Metrics

**Files:**
- Create: `src/czsc_trader/backtest.py`
- Create: `tests/test_backtest.py`

**Interfaces:**
- Consumes: daily OHLC and decision-date target positions.
- Produces: `BacktestResult(portfolio, equity, orders, metrics, windows)` and `run_backtest(daily, target_position, fee_rate=0.0005) -> BacktestResult`.

- [ ] **Step 1: Write failing next-open and hand-calculation tests**

```python
def test_signal_executes_at_next_open():
    result = run_backtest(tiny_daily, pd.Series([0, 1, 1, 0], index=tiny_daily.index))
    assert result.orders.iloc[0]["signal_date"] == tiny_daily.index[1]
    assert result.orders.iloc[0]["execution_date"] == tiny_daily.index[2]
    assert result.orders.iloc[0]["price"] == tiny_daily.loc[tiny_daily.index[2], "open"]


def test_costed_equity_matches_manual_account(tiny_daily):
    result = run_backtest(tiny_daily, tiny_target, fee_rate=0.0005)
    assert result.equity.iloc[-1] == pytest.approx(manual_terminal_value, rel=1e-10)
```

- [ ] **Step 2: Run tests and verify missing-module failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_backtest.py -v`

Expected: fails because `czsc_trader.backtest` does not exist.

- [ ] **Step 3: Implement vectorbt target-percent orders**

Shift decision positions by one row, use the next row's open as order price, and build a vectorbt portfolio with initial cash 1,000,000, `size_type="targetpercent"`, `fees=0.0005`, no shorting, and daily close marking. Extract vectorbt orders and attach the originating signal date.

- [ ] **Step 4: Implement independent ledger and benchmark windows**

Create a second simple cash/share ledger using identical order dates, prices, and fees; assert its daily equity matches vectorbt within `1e-8` relative tolerance. Define windows with start `2025-12-31` and ends `2026-03-31`, `2026-06-30`, `2026-08-21`; compute strategy equity return, close-to-close Buy & Hold return, excess, and PASS/FAIL.

- [ ] **Step 5: Run backtest tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_backtest.py -v`

Expected: vectorbt execution, cost, benchmark, and period slicing tests pass.

- [ ] **Step 6: Commit the backtest layer**

```powershell
git add src/czsc_trader/backtest.py tests/test_backtest.py
git commit -m "feat: verify strategy execution with vectorbt"
```

### Task 5: Audit Trail and Reproducible Research Entry Point

**Files:**
- Create: `src/czsc_trader/audit.py`
- Create: `src/czsc_trader/research.py`
- Create: `scripts/run_research.py`
- Create: `tests/test_audit.py`
- Create: `tests/test_research.py`
- Create: `README.md`

**Interfaces:**
- Consumes: all earlier component results.
- Produces: `run_research(raw_dir: Path, output_dir: Path) -> dict`, CSV/JSON/Markdown artifacts, and process exit code 0 for a valid audited run.

- [ ] **Step 1: Write failing audit tests**

```python
def test_audit_rejects_same_day_execution(valid_orders, valid_selections):
    bad = valid_orders.copy()
    bad.loc[0, "execution_date"] = bad.loc[0, "signal_date"]
    with pytest.raises(AssertionError, match="execution_date"):
        audit_no_lookahead(bad, valid_selections)


def test_research_writes_required_artifacts(tmp_path):
    summary = run_research(Path("data/raw"), tmp_path)
    expected = {"factors.csv", "monthly_parameters.csv", "orders.csv", "equity.csv", "metrics.json", "report.md", "manifest.json"}
    assert expected <= {p.name for p in tmp_path.iterdir()}
    assert set(summary["windows"]) == {"2026Q1", "2026H1", "2026_01_08"}
```

- [ ] **Step 2: Run audit tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_audit.py tests/test_research.py -v`

Expected: fails because audit and research modules do not exist.

- [ ] **Step 3: Implement explicit temporal assertions**

`audit_no_lookahead` must assert every `train_end < as_of_date`, every `signal_date < execution_date`, selection dates are month starts, candidate counts are constant after warmup, positions belong to `{0, 1}`, and future-truncation fingerprints match for fixed checkpoints.

- [ ] **Step 4: Implement output manifest and report**

Write all tables atomically to the supplied output directory. `manifest.json` must contain raw SHA-256 values, Python/CZSC/vectorbt versions, fee rate, data cutoff, candidate-space fingerprint, and UTC run timestamp. `report.md` must show the three target windows first, followed by full-period metrics, factor coverage, monthly selected rules, audit status, and caveats.

- [ ] **Step 5: Add the CLI and usage documentation**

```python
if __name__ == "__main__":
    summary = run_research(Path("data/raw"), Path("outputs/latest"))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
```

Document `.\.venv\Scripts\python.exe scripts\run_research.py` and `.\.venv\Scripts\python.exe -m pytest -q` in `README.md`.

- [ ] **Step 6: Run audit and research tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_audit.py tests/test_research.py -v`

Expected: all audit and artifact tests pass.

- [ ] **Step 7: Commit the research pipeline**

```powershell
git add src/czsc_trader/audit.py src/czsc_trader/research.py scripts/run_research.py tests/test_audit.py tests/test_research.py README.md
git commit -m "feat: add audited research pipeline"
```

### Task 6: End-to-End Run, Target Comparison, and Final Verification

**Files:**
- Modify only if verification exposes a specific defect in files created above.
- Generate: `outputs/latest/*` (ignored by Git).

**Interfaces:**
- Consumes: the completed package and immutable raw data.
- Produces: verified test output and the final empirical PASS/FAIL results.

- [ ] **Step 1: Run the full test suite**

Run: `.\.venv\Scripts\python.exe -m pytest -q`

Expected: zero failures.

- [ ] **Step 2: Run the research pipeline from raw CSVs**

Run: `.\.venv\Scripts\python.exe scripts\run_research.py`

Expected: exits 0 and writes the seven required artifacts under `outputs/latest`.

- [ ] **Step 3: Independently inspect target results and audit manifest**

Run: `.\.venv\Scripts\python.exe -c "import json; from pathlib import Path; p=Path('outputs/latest'); print((p/'report.md').read_text(encoding='utf-8')); print(json.loads((p/'manifest.json').read_text(encoding='utf-8')))"`

Expected: all dates, package versions, hashes, temporal audit status, and each target-window strategy/Buy & Hold return are present.

- [ ] **Step 4: Run code-quality and repository checks**

Run: `.\.venv\Scripts\python.exe -m compileall -q src scripts tests`

Run: `git diff --check`

Run: `git status --short`

Expected: compilation and whitespace checks pass; only intentional source changes are pending, while `outputs/` and `data/raw/` remain ignored.

- [ ] **Step 5: Commit final verified adjustments**

```powershell
git add src scripts tests README.md pyproject.toml
git commit -m "test: verify 588080 walk-forward research"
```

If there are no adjustments after the prior commit, do not create an empty commit.

### Empirical Refinement Recorded During Execution

The first audited run passed 2026Q1 and 2026-01 through August but missed 2026H1 because the factor rule was underweight during the Q2 rally. The approved causal tuning policy was implemented as `apply_annual_alpha_lock`: with at least 252 prior sessions, positive Q1 excess return triggers a full-long target from the Q1-end signal through year-end. Tests prove that it does not change pre-Q1 decisions, and `alpha_locks.csv` records each trigger.
