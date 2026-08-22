# Backtest Interactive Charts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate three offline interactive daily charts that overlay CZSC pens and divergences, factor signals, and actual strategy trades for the independently funded target periods.

**Architecture:** Add a focused `charting.py` module that converts existing validated daily bars, factor outputs, and vectorbt orders into Plotly figures. CZSC 1.0.1 remains the sole source of pens and five/seven-pen divergence labels; research orchestration writes one self-contained HTML per period and records the files in the report and manifest.

**Tech Stack:** Python 3.12, CZSC 1.0.1, Plotly 6.9, pandas 3, pytest

**Spec:** `docs/superpowers/specs/2026-08-23-backtest-interactive-charts-design.md`

## Global Constraints

- Generate exactly `chart_2026Q1.html`, `chart_2026H1.html`, and `chart_2026_01_08.html`.
- Do not display or derive line segments.
- Do not change factors, positions, orders, parameters, or return metrics.
- Never use bars after a chart period's end date.
- Embed Plotly JavaScript so every HTML opens without network access.
- Divergence overlays are explanatory only and come from CZSC `cxt_five_bi_V230619` and `cxt_seven_bi_V230620`.

---

### Task 1: Build causal chart data extraction and Plotly figure

**Files:**
- Create: `src/czsc_trader/charting.py`
- Create: `tests/test_charting.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: validated daily frame with `dt,symbol,open,high,low,close,vol,amount`; factor frame indexed by `dt`; one period's vectorbt orders.
- Produces: `build_period_chart(daily: pd.DataFrame, factors: pd.DataFrame, orders: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, title: str) -> plotly.graph_objects.Figure` and `write_period_chart(..., output_path: Path) -> Path`.

- [ ] **Step 1: Write failing tests for factor transitions, period cutoffs, and required layers**

```python
def test_factor_markers_only_emit_real_position_changes():
    index = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
    factors = pd.DataFrame({"base_target_position": [0.0, 1.0, 0.0]}, index=index)
    markers = extract_factor_markers(factors, index[0], index[-1])
    assert markers[["dt", "side"]].to_dict("records") == [
        {"dt": index[1], "side": "FactorBuy"},
        {"dt": index[2], "side": "FactorExit"},
    ]

def test_chart_has_no_candle_after_period_end(sample_chart_inputs):
    figure = build_period_chart(*sample_chart_inputs, start=pd.Timestamp("2026-01-05"), end=pd.Timestamp("2026-01-07"), title="test")
    candle = next(trace for trace in figure.data if trace.name == "日K")
    assert max(pd.to_datetime(candle.x)) == pd.Timestamp("2026-01-07")

def test_chart_contains_required_explanatory_layers(sample_chart_inputs):
    figure = build_period_chart(*sample_chart_inputs, start=START, end=END, title="test")
    names = {trace.name for trace in figure.data}
    assert {"日K", "CZSC笔", "底背驰", "顶背驰", "因子入场", "因子离场", "策略买入", "策略卖出", "structure", "trend", "volume_position", "factor_score"} <= names
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_charting.py`

Expected: FAIL because `czsc_trader.charting` does not exist.

- [ ] **Step 3: Add Plotly as a direct dependency and implement chart extraction**

Add `"plotly==6.9.0"` to project dependencies. In `charting.py`, define:

```python
DIVERGENCE_CONFIG = [
    {"name": "cxt_five_bi_V230619", "freq": "日线", "di": 1},
    {"name": "cxt_seven_bi_V230620", "freq": "日线", "di": 1},
]

def extract_factor_markers(factors, start, end):
    position = factors["base_target_position"].astype(float)
    changed = position.ne(position.shift(1)) & position.index.to_series().between(start, end)
    rows = [{"dt": dt, "side": "FactorBuy" if position.loc[dt] == 1 else "FactorExit"} for dt in position.index[changed] if dt > position.index[0]]
    return pd.DataFrame(rows, columns=["dt", "side"])
```

Build CZSC with bars truncated at `end`, read `bi_list` endpoints for the pen line, generate the two historical divergence signals with `czsc.generate_czsc_signals`, retain values containing `底背驰` or `顶背驰`, and align all markers to the daily close/high/low inside `[start, end]`.

- [ ] **Step 4: Implement the two-panel Plotly figure and offline writer**

Use `make_subplots(rows=2, shared_xaxes=True, row_heights=[0.72, 0.28])`. Add candlesticks, a blue pen line, separate bottom/top divergence markers, factor-entry/factor-exit markers, actual Buy/Sell order markers, and four factor lines. Always add each named trace, even when its data is empty. Write with:

```python
figure.write_html(output_path, include_plotlyjs=True, full_html=True)
```

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_charting.py`

Expected: all charting tests PASS.

- [ ] **Step 6: Commit the chart module**

```powershell
git add pyproject.toml src/czsc_trader/charting.py tests/test_charting.py
git commit -m "feat: render CZSC backtest charts"
```

### Task 2: Export three chart artifacts from the research pipeline

**Files:**
- Modify: `src/czsc_trader/research.py`
- Modify: `tests/test_research.py`

**Interfaces:**
- Consumes: `write_period_chart` from Task 1 and the already-created `period_results` mapping.
- Produces: three HTML files, report links, and `manifest["charts"]` metadata.

- [ ] **Step 1: Extend the integration test and verify it fails**

Add the three chart names to the expected artifact set and assert:

```python
for name in ("2026Q1", "2026H1", "2026_01_08"):
    html = (tmp_path / f"chart_{name}.html").read_text(encoding="utf-8")
    assert "plotly" in html.lower()
    assert "CZSC笔" in html
    assert "策略买入" in html

manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
assert manifest["charts"]["files"] == [
    "chart_2026Q1.html", "chart_2026H1.html", "chart_2026_01_08.html"
]
```

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_research.py`

Expected: FAIL because chart files and manifest metadata are missing.

- [ ] **Step 2: Generate each period chart after authoritative orders exist**

For each `TARGET_PERIODS` entry, pass the actual period bounds and `result.orders` to `write_period_chart`. Store file names in deterministic target-period order. Do not read exported CSVs back into memory.

- [ ] **Step 3: Add report and manifest metadata**

Add a `## 交互式日线图` section to the report with relative links. Add:

```python
"charts": {
    "files": chart_files,
    "plotly": version("plotly"),
    "divergence_signals": ["cxt_five_bi_V230619", "cxt_seven_bi_V230620"],
    "data_policy": "warm-up allowed before period start; no bars after period end",
}
```

- [ ] **Step 4: Run integration and regression tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_research.py tests/test_backtest.py tests/test_audit.py`

Expected: all tests PASS; all three target windows retain their previous returns.

- [ ] **Step 5: Commit research integration**

```powershell
git add src/czsc_trader/research.py tests/test_research.py
git commit -m "feat: export charts with research evidence"
```

### Task 3: Document, verify, and generate the formal revision

**Files:**
- Modify: `README.md`
- Generated/ignored: `outputs/588080_0823_RXX/chart_*.html`

**Interfaces:**
- Consumes: the complete research command.
- Produces: a verified new revision directory containing three usable offline HTML charts.

- [ ] **Step 1: Document chart files and semantics**

State that each period has its own offline HTML, pens are CZSC structures confirmed by the period end, divergence overlays are explanatory, factor markers use `base_target_position`, and trade markers come from vectorbt orders.

- [ ] **Step 2: Install the updated editable project and run the full suite**

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: complete suite PASS and `pip check` reports no broken requirements.

- [ ] **Step 3: Generate the next formal research revision**

```powershell
.\.venv\Scripts\python.exe scripts\run_research.py
```

Expected: the next `outputs/588080_0823_RXX` contains all prior evidence plus three chart HTML files; audit is PASS and all target windows pass.

- [ ] **Step 4: Render and inspect all three HTML files**

Open each local HTML in a browser at desktop width. Verify visible candles, pen line, legends, factor panel, and any available divergence/factor/trade markers. Confirm no browser console error and no chart includes a date after its period end.

- [ ] **Step 5: Run final machine checks and commit**

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m compileall -q src scripts
git diff --check
git add README.md docs/superpowers/plans/2026-08-23-backtest-interactive-charts.md
git commit -m "docs: document interactive backtest charts"
```
