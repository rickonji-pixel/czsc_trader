# EX07 In-Memory Performance Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an isolated Optuna `InMemoryStorage` benchmark path and measure 16, 256, and 1024 Trial end-to-end throughput without changing EX07 research artifacts or accessing 2026 data.

**Architecture:** Preserve the existing EX07 SQLite study path and factor/backtest semantics. Add optional per-batch timing to the generic ask/evaluate/tell loop, create a separate in-memory benchmark module and CLI, and write engineering-only JSON results below ignored `outputs/benchmarks/`.

**Tech Stack:** Python 3.12, Optuna 4.9.0, Joblib 1.5.3/loky, pandas, vectorbt 1.1.0, pytest.

**Spec:** `docs/superpowers/specs/2026-08-24-ex07-inmemory-performance-design.md`

## Global Constraints

- Selection data must end at `2025-12-31`; no benchmark code may call the 2026 holdout path.
- Preserve the 91 EX06 candidates, 94-dimensional Trial parameterization, TPE seed/configuration, projection, objective, batch size 8, and ordered ask/tell semantics.
- The formal EX07 entry point must continue to use SQLite and remain resumable.
- In-memory mode must not create or read `experiments/0824_EX07/runtime/optuna.db`.
- Do not modify tracked files below `experiments/0824_EX07/`.
- Develop on the current `master`; do not create a worktree or use subagents.
- Benchmark outputs are engineering evidence under ignored `outputs/benchmarks/`, not research PASS/FAIL artifacts.

---

### Task 1: Instrument the batched ask/evaluate/tell loop

**Files:**
- Modify: `src/czsc_trader/optuna_search.py:358-422`
- Modify: `tests/test_optuna_search.py`

**Interfaces:**
- Consumes: existing `run_study_batches(...)` arguments.
- Produces: optional keyword `collect_batch_timings: bool = False`; when enabled, the returned mapping includes `batch_timings: list[dict[str, float | int]]`.

- [ ] **Step 1: Write failing timing tests**

Add a test that runs two four-Trial synthetic batches with `collect_batch_timings=True` and asserts:

```python
result = run_study_batches(
    study,
    _request,
    _evaluate,
    maximum_completed_trials=8,
    minimum_completed_trials=8,
    no_improvement_trials=8,
    maximum_wall_time_seconds=60,
    batch_size=4,
    collect_batch_timings=True,
)
assert len(result["batch_timings"]) == 2
assert set(result["batch_timings"][0]) == {
    "batch_number",
    "first_trial_number",
    "trial_count",
    "ask_and_project_seconds",
    "parallel_evaluate_seconds",
    "tell_and_attrs_seconds",
    "batch_total_seconds",
}
assert all(value >= 0 for key, value in result["batch_timings"][0].items() if key.endswith("_seconds"))
```

Add a compatibility assertion that the `batch_timings` key is absent when the flag is omitted.

- [ ] **Step 2: Run tests and verify RED**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_optuna_search.py -q`

Expected: failure because `run_study_batches` does not accept `collect_batch_timings`.

- [ ] **Step 3: Implement minimal timing collection**

Use `perf_counter()` immediately around the request-creation loop, batch evaluator call, and ordered attribute/tell loop. Append one mapping per completed batch only when collection is enabled. Keep existing stop checks and result ordering unchanged.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_optuna_search.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add -- src/czsc_trader/optuna_search.py tests/test_optuna_search.py
git commit -m "perf: instrument Optuna batch phases"
```

### Task 2: Add explicit in-memory Study creation

**Files:**
- Modify: `src/czsc_trader/optuna_runner.py:343-375`
- Modify: `tests/test_optuna_runner.py`

**Interfaces:**
- Consumes: `_study_from_protocol(runtime_dir, protocol, protocol_digest, *, storage_mode="sqlite")`.
- Produces: an Optuna Study backed by SQLite for `sqlite` and `optuna.storages.InMemoryStorage` for `memory`.

- [ ] **Step 1: Write failing storage-isolation tests**

Create a minimal valid protocol fixture and assert:

```python
study = _study_from_protocol(tmp_path / "runtime", protocol, "digest", storage_mode="memory")
assert isinstance(study._storage, optuna.storages.InMemoryStorage)
assert not (tmp_path / "runtime").exists()
```

Retain a separate SQLite assertion that the default mode creates `runtime/optuna.db`. Add an invalid-mode assertion for `ValueError("unsupported storage mode")`.

- [ ] **Step 2: Run test and verify RED**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_optuna_runner.py -k "storage" -q`

Expected: failure because `storage_mode` is not accepted.

- [ ] **Step 3: Implement the storage switch**

For `memory`, pass a new `optuna.storages.InMemoryStorage()` to `optuna.create_study` and skip `runtime_dir.mkdir`. For `sqlite`, preserve the exact current database URI and `load_if_exists=True`. Apply the same study attributes in both modes. The formal runner call at `run_optuna_experiment` must pass `storage_mode="sqlite"` explicitly.

- [ ] **Step 4: Run Optuna runner tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_optuna_runner.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add -- src/czsc_trader/optuna_runner.py tests/test_optuna_runner.py
git commit -m "feat: add isolated in-memory Optuna storage"
```

### Task 3: Build the engineering benchmark runner and CLI

**Files:**
- Create: `src/czsc_trader/optuna_benchmark.py`
- Create: `scripts/benchmark_optuna.py`
- Create: `tests/test_optuna_benchmark.py`

**Interfaces:**
- Produces: `run_inmemory_benchmark(raw_dir: Path, baseline_root: Path, experiment_dir: Path, output_root: Path, *, completed_trials: int) -> dict[str, object]`.
- Produces: `summarize_batch_timings(rows: Sequence[Mapping[str, float | int]]) -> dict[str, object]`.
- CLI: `python scripts/benchmark_optuna.py --trials N [--output-root PATH]` and prints `benchmark_path=<actual path>`.

- [ ] **Step 1: Write failing unit tests**

Test `summarize_batch_timings` with deterministic synthetic timings and assert total, mean, median, P95, maximum, and phase shares. Test the runner with monkeypatched selection inputs, Study creation, batch loop, and file writer; assert it requests `storage_mode="memory"`, passes `collect_batch_timings=True`, rejects non-positive or non-multiple-of-eight Trial counts, and never references `_run_holdout` or `_freeze_winner`.

Assert the result contains the fixed identity fields and positive calculated metrics:

```python
assert result["benchmark_kind"] == "engineering_only_ex07_inmemory"
assert result["completed_trials"] == 16
assert result["selection_sample_end"] == "2025-12-31"
assert result["trial_zero_objective"] == 0.0
assert result["trials_per_hour"] > 0.0
assert result["speedup_vs_ex07"] > 0.0
assert result["estimated_seconds_for_4096"] > 0.0
assert result["timings"]["batch_count"] == 2
assert result["environment"]["optuna"] == "4.9.0"
```

- [ ] **Step 2: Run tests and verify RED**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_optuna_benchmark.py -q`

Expected: import failure because `czsc_trader.optuna_benchmark` does not exist.

- [ ] **Step 3: Implement timing summary**

Implement deterministic summary functions using the standard library and NumPy. P95 must use `np.percentile(values, 95)`. Phase shares divide phase totals by total batch time and return zero only when total time is zero.

- [ ] **Step 4: Implement the benchmark runner**

Reuse `prepare_selection_inputs`, `_study_from_protocol(..., storage_mode="memory")`, `trial_params_for_strategy`, `enqueue_initial_trial`, `suggest_trial_parameters`, `TrialEvaluationInputs`, `evaluate_trial_batch`, and `run_study_batches`. Set all three completion limits so exactly `completed_trials` finish, while retaining batch size 8. Validate `context.data.daily["dt"].max() <= Timestamp("2025-12-31")` before creating the Study.

After completion, verify Trial 0 has value exactly `0.0`. Record versions with `importlib.metadata.version`, logical CPUs with `os.cpu_count`, actual command with `sys.argv`, and baseline throughput `301.95`. Write JSON through a temporary sibling followed by `Path.replace` so incomplete files are not published.

- [ ] **Step 5: Implement the CLI**

The script resolves all defaults relative to `Path(__file__).resolve().parents[1]`, not the process working directory. Default output root is `<repo>/outputs/benchmarks`. It calls the benchmark runner once and prints the returned output path and throughput.

- [ ] **Step 6: Run benchmark tests and verify GREEN**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_optuna_benchmark.py tests/test_optuna_search.py tests/test_optuna_runner.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```powershell
git add -- src/czsc_trader/optuna_benchmark.py scripts/benchmark_optuna.py tests/test_optuna_benchmark.py
git commit -m "feat: add EX07 in-memory performance benchmark"
```

### Task 4: Run correctness gate and three performance benchmarks

**Files:**
- Runtime only: `outputs/benchmarks/*.json` (Git ignored)

**Interfaces:**
- Consumes: `scripts/benchmark_optuna.py`.
- Produces: actual benchmark paths and JSON reports for 16, 256, and 1024 completed Trial runs.

- [ ] **Step 1: Run the 16-Trial correctness benchmark**

Run: `./.venv/Scripts/python.exe scripts/benchmark_optuna.py --trials 16`

Require: exit 0, selection cutoff `2025-12-31`, Trial 0 objective `0.0`, two timing rows, no file change under tracked `experiments/0824_EX07/`.

- [ ] **Step 2: Run the 256-Trial startup benchmark**

Run: `./.venv/Scripts/python.exe scripts/benchmark_optuna.py --trials 256`

Require: exit 0, 256 completed Trial, zero failed Trial, 32 timing rows.

- [ ] **Step 3: Run the 1024-Trial full benchmark**

Run: `./.venv/Scripts/python.exe scripts/benchmark_optuna.py --trials 1024`

Require: exit 0, 1024 completed Trial, zero failed Trial, 128 timing rows. If runtime exceeds 60 seconds, post a user-facing progress update at least once per minute while waiting.

- [ ] **Step 4: Analyze measured performance**

Read the three returned JSON paths. Report total seconds, Trial/hour, speedup against 301.95 Trial/hour, phase shares, TPE slowdown after Trial 256, and estimated 4096 duration. Do not infer strategy PASS/FAIL from benchmark objectives.

### Task 5: Regression verification and delivery commit

**Files:**
- Modify only if required by verified failures from Tasks 1-4.

**Interfaces:**
- Produces: evidence that existing EX07 behavior and the project test suite remain healthy.

- [ ] **Step 1: Run focused tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_optuna_search.py tests/test_optuna_runner.py tests/test_optuna_benchmark.py -q`

Expected: all pass.

- [ ] **Step 2: Run the project test command**

Run: `./.venv/Scripts/python.exe -m pytest -q`

Expected: all collected tests pass.

- [ ] **Step 3: Run static and dependency checks**

```powershell
./.venv/Scripts/python.exe -m compileall -q src scripts tests
./.venv/Scripts/python.exe -m pip check
git diff --check
```

Expected: all commands exit 0 and pip reports no broken requirements.

- [ ] **Step 4: Confirm isolation and commit remaining tracked changes**

Run `git status --short`, confirm no tracked EX07 artifact changed and `outputs/benchmarks/` remains ignored. If tracked delivery changes remain from verified fixes, commit only the known delivery files with:

```powershell
git add -- src/czsc_trader/optuna_search.py src/czsc_trader/optuna_runner.py src/czsc_trader/optuna_benchmark.py scripts/benchmark_optuna.py tests/test_optuna_search.py tests/test_optuna_runner.py tests/test_optuna_benchmark.py
git commit -m "test: verify EX07 in-memory benchmark"
```

- [ ] **Step 5: Deliver measured results**

Report commits, changed files, exact verification commands and outcomes, actual ignored benchmark paths, measured throughput, phase breakdown, limitations of pure memory, and the fact that no 2026 data or formal holdout was accessed.
