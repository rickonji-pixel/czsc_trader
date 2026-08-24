# EX06 Event-Aware Parallel Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct sparse CZSC event admission and run the unchanged EX05 search with deterministic Joblib parallelism and exact position caching.

**Architecture:** Extend the factor-discovery module with explicit state/event classification, independent-event counting, canonical aliases, and signal-support metadata. Keep coordinate optimization sequential; parallelize only the 32 independent rolling fits, with workers returning data and the parent writing every artifact.

**Tech Stack:** Python 3.12, CZSC 1.0.1, vectorbt 1.1.0, pandas, NumPy, Joblib 1.5.3, pytest.

**Spec:** `experiments/0824_EX06/02_design.md`

## Global Constraints

- No 2026 file may be opened before the EX06 challenger is written and hashed.
- Selection and PASS use strategy return only; Sharpe is report-only.
- Keep the EX05 64-spec grid, rolling windows, ranking tuple, sparse constraints, four-layer score, and sequential coordinate updates unchanged.
- Event factors bypass the 80% coverage, 30-day activation, and 90% maximum-ratio state filters.
- Do not implement custom second-buy, second-sell, or third-sell definitions missing from CZSC 1.0.1.
- Use Joblib 1.5.3 with `loky`; do not add staged search, Optuna, Dask, or Ray.
- Workers never write files. Serial and parallel results must select identical specs, weights, and positions; metric error must be at most `1e-12`.

---

### Task 1: Event-aware candidate admission

**Files:**
- Modify: `src/czsc_trader/factor_discovery.py:1-190`
- Modify: `tests/test_factor_discovery.py`

**Interfaces:**
- Produces: `is_event_primary(primary: str) -> bool`
- Produces: `independent_event_count(indicator: pd.Series) -> int`
- Extends: `build_candidate_factors(...) -> CandidateFactors` metadata with `factor_kind`, `independent_events`, `years`, `half_year_windows`, `status`, and `canonical_factor`.
- Extends: `generate_candidate_factors(...) -> CandidateFactors` with first-sell generation and `signal_support` metadata.

- [ ] **Step 1: Write failing event classification tests**

```python
def test_event_primary_and_independent_occurrences() -> None:
    indicator = pd.Series([0, 1, 1, 0, 1, 0, 0, 1], dtype=float)
    assert is_event_primary("一买")
    assert is_event_primary("类三卖")
    assert is_event_primary("aAb式底背驰")
    assert is_event_primary("类趋势顶背驰")
    assert not is_event_primary("向上")
    assert not is_event_primary("其他")
    assert independent_event_count(indicator) == 3
```

- [ ] **Step 2: Write the sparse-event regression test**

```python
def test_sparse_event_is_retained_but_sparse_state_is_filtered() -> None:
    index = pd.date_range("2021-01-01", periods=40, freq="D")
    raw = pd.DataFrame({
        "raw__daily__event": ["其他_x"] * 39 + ["一买_x"],
        "raw__daily__state": ["其他_x"] * 39 + ["向上_x"],
    }, index=index)
    base = pd.DataFrame({"base": np.ones(40)}, index=index)
    result = build_candidate_factors(raw, base, pd.Series({"base": 1.0}), _protocol(state_min_active_days=30))
    assert "state__raw__daily__event::一买" in result.factors
    assert "state__raw__daily__state::向上" not in result.factors
```

- [ ] **Step 3: Run the focused tests and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_factor_discovery.py -v`

Expected: FAIL because event helpers and event-specific admission do not exist.

- [ ] **Step 4: Implement classification and occurrence counting**

```python
EVENT_PRIMARY_TOKENS = ("买", "卖", "底背驰", "顶背驰")

def is_event_primary(primary: str) -> bool:
    return primary not in {"其他", "任意", "中性"} and any(x in primary for x in EVENT_PRIMARY_TOKENS)

def independent_event_count(indicator: pd.Series) -> int:
    active = indicator.fillna(0.0).astype(bool)
    return int((active & ~active.shift(fill_value=False)).sum())
```

Apply existing coverage/day/ratio gates only to non-events. Require at least one independent event and record calendar years and half-year windows.

- [ ] **Step 5: Test and implement canonical duplicate aliases**

Create two event columns with identical indicators but different primary names. Assert only the name-sorted canonical factor enters the matrix while both metadata rows remain, with statuses `candidate` and `aliased_duplicate` and the alias pointing to `canonical_factor`.

- [ ] **Step 6: Test and implement the CZSC support audit**

Compare protocol `event_signal_requirements` with `czsc._native.list_signal_names()`. Add daily `cxt_first_sell_V221126`; record each requirement as `available`, `generated`, `observed`, `aliased_duplicate`, or `unavailable_in_czsc_1_0_1` in `metadata["signal_support"]`.

- [ ] **Step 7: Verify and commit**

Run: `.venv\Scripts\python.exe -m pytest tests/test_factor_discovery.py -v`

Expected: all factor-discovery tests PASS.

```powershell
git add src/czsc_trader/factor_discovery.py tests/test_factor_discovery.py
git commit -m "research: admit sparse CZSC event factors"
```

### Task 2: Existing target-position cache diagnostics

**Files:**
- Modify: `src/czsc_trader/four_layer_runner.py:148-174`
- Modify: `tests/test_four_layer_runner.py`

**Interfaces:**
- Reuses: existing `_target_digest(target: pd.Series) -> str` and process-local cache.
- Extends: `_PeriodEvaluator.evaluate(target)` with hit and miss counters.
- Produces: `_PeriodEvaluator.cache_stats -> dict[str, int]`.

- [ ] **Step 1: Write a failing cache test**

Monkeypatch `run_period_backtests`, call the existing `_PeriodEvaluator.evaluate` twice with copied but identical positions, and assert the backtest runs once and stats equal `{"hits": 1, "misses": 1, "entries": 1}`.

- [ ] **Step 2: Run the cache test and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_four_layer_runner.py -k cache_stats -v`

Expected: FAIL because the existing cache does not expose statistics.

- [ ] **Step 3: Add statistics to the existing cache**

Initialize `_cache_hits` and `_cache_misses` beside the existing `cache` dictionary. Increment the appropriate counter around the existing digest lookup and expose a copied stats dictionary. Do not change `_target_digest`, cache keys, metric values, or cache lifetime.

- [ ] **Step 4: Verify and commit**

Run: `.venv\Scripts\python.exe -m pytest tests/test_four_layer_runner.py tests/test_factor_discovery_runner.py -v`

Expected: all runner tests PASS.

```powershell
git add src/czsc_trader/four_layer_runner.py tests/test_four_layer_runner.py experiments/0824_EX06/02_design.md experiments/0824_EX06/implementation_plan.md
git commit -m "research: record factor evaluator cache diagnostics"
```

### Task 3: Deterministic Joblib outer parallelism

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/czsc_trader/factor_discovery_runner.py:171-330`
- Modify: `tests/test_factor_discovery_runner.py`

**Interfaces:**
- Produces: `RollingFitTask(step: float, rounds: int, validation: str)` and `.key`.
- Produces: frozen `RollingFitInputs` containing read-only factor values, index, factor names, origin weights, EX04 target, EX04 thresholds, state rule, daily prices, half-year periods, fee, cash, and protocol.
- Produces: `_rolling_fit_tasks(protocol) -> tuple[RollingFitTask, ...]`.
- Produces: `_fit_rolling_weights(inputs: RollingFitInputs, tasks: Sequence[RollingFitTask], n_jobs: int) -> dict[tuple[float, int, str], pd.Series]`.
- Produces: `_benchmark_parallel_jobs(inputs: RollingFitInputs, choices: Sequence[int]) -> dict[str, object]`.

- [ ] **Step 1: Pin Joblib directly**

Add `"joblib==1.5.3"` to `pyproject.toml`, then run `.venv\Scripts\python.exe -m pip check` and expect no broken requirements.

- [ ] **Step 2: Write failing task-boundary tests**

```python
def test_rolling_fit_tasks_are_exact_and_deterministic() -> None:
    tasks = _rolling_fit_tasks(_protocol())
    assert len(tasks) == 32
    assert len({task.key for task in tasks}) == 32
    assert tasks == tuple(sorted(tasks, key=lambda task: task.key))
```

Add an injected fake worker test that calls `_fit_rolling_weights` with `n_jobs=1` and `n_jobs=2`, then compares ordered keys and every returned Series with `check_exact=True`.

- [ ] **Step 3: Run runner tests and verify missing interfaces**

Run: `.venv\Scripts\python.exe -m pytest tests/test_factor_discovery_runner.py -v`

Expected: FAIL on missing task and parallel interfaces.

- [ ] **Step 4: Implement ordered dispatch**

```python
with parallel_config(backend="loky", inner_max_num_threads=1, max_nbytes="100K", mmap_mode="r"):
    rows = Parallel(n_jobs=n_jobs)(
        delayed(_run_rolling_fit_task)(task, inputs) for task in ordered_tasks
    )
result = dict(sorted(rows, key=lambda row: row[0]))
```

Allow only `n_jobs` 1, 2, 4, or 8. Pass factor values as a contiguous read-only NumPy array for memmapping and reconstruct labels in workers. Workers receive no output paths or writers. Reject missing, duplicate, or unknown task keys.

- [ ] **Step 5: Replace only the outer fitted-cache loop**

Build all 32 fitted weights before the 64-spec threshold loop. Continue retrieving weights by `(step, rounds, validation)` and evaluating thresholds in the parent. Do not parallelize coordinate updates.

- [ ] **Step 6: Implement the fixed benchmark**

Benchmark choices 1, 2, 4, 8 on the four tasks using step 0.025, one round, and validations 2022H1 through 2023H2. Compare all weights exactly with the serial result; select minimum `(elapsed_seconds, n_jobs)`. Return backend, version, choices, selected count, equivalence flag, and timings.

- [ ] **Step 7: Verify and commit**

Run: `.venv\Scripts\python.exe -m pytest tests/test_factor_discovery.py tests/test_factor_discovery_runner.py -v`

Expected: all focused tests PASS.

```powershell
git add pyproject.toml src/czsc_trader/factor_discovery_runner.py tests/test_factor_discovery_runner.py
git commit -m "perf: parallelize independent rolling factor fits"
```

### Task 4: Preregister and execute EX06

**Files:**
- Create: `experiments/0824_EX06/artifacts/protocol.json`
- Modify: `src/czsc_trader/factor_discovery_runner.py:503-611`
- Modify: `tests/test_factor_discovery_runner.py`
- Modify: `experiments/0824_EX06/03_execution.md`
- Modify: `experiments/0824_EX06/04_conclusion.md`
- Create: generated evidence under `experiments/0824_EX06/artifacts/`
- Create: `experiments/0824_EX06/experiment_manifest.json`

**Interfaces:**
- Extends: `validate_factor_protocol(protocol)` with event and parallel fields.
- Extends: `run_factor_discovery_experiment(..., ex05_path: Path | None = None) -> dict`.
- Changes CLI default to `experiments/0824_EX06`.

- [ ] **Step 1: Create the exact protocol**

Copy all EX05 grids and constraints. Set type to `event_aware_parallel_factor_discovery`; append `cxt_first_sell_V221126` to `new_daily_signals`; and preregister these exact support-audit names:

```json
[
  "cxt_first_buy_V221126",
  "cxt_first_sell_V221126",
  "cxt_second_buy_V230320",
  "cxt_second_sell_V230320",
  "cxt_third_buy_V230228",
  "cxt_third_sell_V230228",
  "cxt_five_bi_V230619",
  "cxt_seven_bi_V230620"
]
```

Also set event minimum 1, backend `loky`, choices `[1, 2, 4, 8]`, inner threads 1, SHA-256 position cache, and `staged_search: false`.

- [ ] **Step 2: Add protocol and holdout-boundary tests**

Reject changed grids, event minimum other than 1, non-loky backend, missing job choices, staged search, Sharpe selection, and holdout-before-freeze. Assert support and benchmark artifacts are parent-written and no 2026 path is opened before the frozen hash exists.

- [ ] **Step 3: Complete runner outputs**

Write `event_signal_support.json`, `parallel_benchmark.json`, cache stats, frozen candidate, and selection evidence. During holdout report EX04, EX05, legacy champion, Buy & Hold, and EX06 while keeping PASS based only on EX04 versus EX06 returns.

- [ ] **Step 4: Run focused regression tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_factor_discovery.py tests/test_factor_discovery_runner.py tests/test_return_only_runner.py tests/test_backtest.py tests/test_experiment_archive.py -v`

Expected: all selected tests PASS.

- [ ] **Step 5: Commit implementation and preregistration before formal execution**

```powershell
git add pyproject.toml src/czsc_trader/factor_discovery.py src/czsc_trader/factor_discovery_runner.py tests/test_factor_discovery.py tests/test_factor_discovery_runner.py experiments/0824_EX06/artifacts/protocol.json
git commit -m "research: preregister EX06 event-aware search"
```

- [ ] **Step 6: Execute the formal experiment once**

Run: `.venv\Scripts\python.exe -m czsc_trader.factor_discovery_runner --experiment-dir experiments/0824_EX06`

Expected: one PASS or FAIL JSON result; the frozen challenger is written and hashed before 2026 is loaded.

- [ ] **Step 7: Run minimal delivery verification**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_factor_discovery.py tests/test_factor_discovery_runner.py tests/test_experiment_archive.py -q
.venv\Scripts\python.exe -m compileall -q src
.venv\Scripts\python.exe -m pip check
git diff --check
```

Verify serial/parallel equivalence, support statuses, absence of 2026 selection hashes, freeze order, causal audit, and all three EX06-versus-EX04 decisions.

- [ ] **Step 8: Record and commit the result without retuning**

Update execution and conclusion documents with benchmark, cache, candidate, freeze, audit, support, return, and Sharpe evidence. Build and validate the manifest, then commit every PASS or FAIL artifact.

```powershell
git add experiments/0824_EX06
git commit -m "research: record EX06 event-aware search results"
```
