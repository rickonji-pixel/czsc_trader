# Champion Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run the preregistered `0824_EX02` diagnostic that attributes the frozen 588080 champion's factor, mapping, weight, threshold, and state-machine effects without reading 2026 or selecting a challenger.

**Architecture:** Refactor the existing causal factor generator to expose deterministic raw, mapped, and grouped signal views without changing champion output. Add a pure attribution module for counterfactual construction, Shapley calculations, stability classification, and dominance checks, plus a runner that reuses the existing baseline resolver and period backtester. Extend the single experiment entrypoint to finalize the already preregistered archive and regenerate its manifest.

**Tech Stack:** Python 3.12, pandas, NumPy, CZSC 1.0.1, vectorbt 1.1.0, Plotly 6.9.0, pytest.

**Spec:** `experiments/0824_EX02/02_design.md`

## Global Constraints

- Work on `research/champion-attribution`; do not use a worktree or subagent.
- Resolve exactly `baseline_20260823` and verify SHA-256 `682be53a7b589a165d325be0c5a309765193d988301cd88f29b6111fefe3f5d5`.
- Load market data with cutoff `2025-12-31`; no 2026 file may be opened or hashed.
- Do not alter the baseline, normal backtest behavior, previous experiment archive, or holdout entrypoint behavior.
- Do not generate `frozen_challenger.json`, select a challenger, or update the champion.
- Use 2020 only for warm-up; use 2021-2025 annual windows for classification and half-year windows for diagnostics.
- Primary metrics are strategy return and Sharpe; materiality thresholds are `0.001` and `0.02`, with at least four of five years required for stable classification.
- Formal outputs belong only under `experiments/0824_EX02/artifacts/`; `outputs/` remains untouched.
- Use TDD for production changes and run only focused tests until final verification.

---

### Task 1: Expose Deterministic Factor Internals

**Files:**
- Modify: `src/czsc_trader/factors.py`
- Modify: `tests/test_factors.py`

**Interfaces:**
- Produces: `signal_primary(value: object) -> str | None`
- Produces: `map_signal_frame(raw: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]`
- Produces: `signal_groups(columns: Iterable[str]) -> dict[str, tuple[str, ...]]`
- Produces: `aggregate_signal_groups(mapped: pd.DataFrame, groups: Mapping[str, Sequence[str]]) -> pd.DataFrame`
- Preserves: `generate_factor_frame(data: MarketData) -> FactorResult` and all existing `FactorResult.frame` columns and values.

- [ ] **Step 1: Write failing tests for public signal parsing, mapping, grouping, and exact backward compatibility**

```python
def test_signal_internals_are_explicit_and_preserve_group_scores(market_data):
    result = generate_factor_frame(market_data)
    raw = result.frame.filter(like="raw__")
    mapped, unknown = map_signal_frame(raw)
    groups = signal_groups(mapped.columns)
    rebuilt = aggregate_signal_groups(mapped, groups)
    pd.testing.assert_frame_equal(
        rebuilt[["structure", "trend", "volume_position"]],
        result.frame[["structure", "trend", "volume_position"]],
    )
    assert signal_primary("多头_任意_任意_0") == "多头"
    assert unknown == result.unknown_values
```

- [ ] **Step 2: Run the new factor test and verify it fails because the public helpers do not exist**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_factors.py`

- [ ] **Step 3: Refactor the existing `_signal_score` path into the four public pure helpers**

Implement helpers by moving, not changing, current keyword and volume-rank semantics. `generate_factor_frame` must call these helpers so there is one mapping implementation.

- [ ] **Step 4: Run factor and baseline snapshot tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_factors.py tests/test_baseline_snapshot.py`
Expected: all pass and the frozen baseline snapshot remains byte-for-byte equivalent at the metric boundary.

- [ ] **Step 5: Commit the factor introspection boundary**

```powershell
git add src/czsc_trader/factors.py tests/test_factors.py
git commit -m "refactor: expose factor attribution inputs"
```

### Task 2: Implement Pure Attribution Mathematics and Classification

**Files:**
- Create: `src/czsc_trader/attribution.py`
- Create: `tests/test_attribution.py`

**Interfaces:**
- Produces: `all_coalitions(names: Sequence[str]) -> tuple[frozenset[str], ...]`
- Produces: `shapley_values(utilities: Mapping[frozenset[str], float], names: Sequence[str]) -> dict[str, float]`
- Produces: `shapley_interactions(utilities: Mapping[frozenset[str], float], names: Sequence[str]) -> dict[tuple[str, str], float]`
- Produces: `classify_annual_effect(rows: pd.DataFrame, return_epsilon: float, sharpe_epsilon: float, stable_years: int) -> str`
- Produces: `dominant_difference_share(champion_target: pd.Series, counterfactual_target: pd.Series, daily_return_delta: pd.Series) -> float`
- Produces: pure builders for group, signal, state, weight, threshold, and state-machine counterfactuals.

- [ ] **Step 1: Write failing exact-math and classification tests**

```python
def test_exact_shapley_values_sum_to_grand_coalition_utility():
    names = ("a", "b", "c")
    utilities = {coalition: float(len(coalition)) for coalition in all_coalitions(names)}
    values = shapley_values(utilities, names)
    assert values == {"a": 1.0, "b": 1.0, "c": 1.0}
    assert sum(values.values()) == utilities[frozenset(names)] - utilities[frozenset()]

def test_four_material_years_classify_stable_negative():
    rows = pd.DataFrame({
        "return_delta": [0.01, 0.02, 0.03, 0.04, -0.01],
        "sharpe_delta": [0.1, 0.2, 0.3, 0.4, -0.1],
        "dominant_share": [0.2] * 5,
    })
    assert classify_annual_effect(rows, 0.001, 0.02, 4) == "stable_negative"
```

Also test interaction indices, redundant/mixed/regime/inconclusive classifications, sample dominance at exactly `0.5`, weight sums, unchanged non-target parameters, and deterministic coalition ordering.

- [ ] **Step 2: Run attribution tests and verify import failures**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_attribution.py`

- [ ] **Step 3: Implement the minimal pure attribution module**

Use exact factorial weights for three-factor Shapley values and the standard pairwise Shapley interaction index. Treat an all-cash coalition's return and Sharpe as zero. Builders must return new frames or `Rule` values and never mutate the champion inputs.

- [ ] **Step 4: Run pure attribution tests and rules tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_attribution.py tests/test_rules.py`

- [ ] **Step 5: Commit the attribution primitives**

```powershell
git add src/czsc_trader/attribution.py tests/test_attribution.py
git commit -m "research: add deterministic attribution primitives"
```

### Task 3: Build the Champion Attribution Runner

**Files:**
- Create: `src/czsc_trader/attribution_runner.py`
- Create: `tests/test_attribution_runner.py`

**Interfaces:**
- Consumes: factor helper APIs from Task 1 and attribution primitives from Task 2.
- Produces: `run_champion_attribution(raw_dir: Path, baseline_root: Path, artifacts_dir: Path, protocol: Mapping[str, object]) -> dict[str, object]`
- Produces artifacts declared in `02_design.md`, including `metrics.json` and one offline `attribution_heatmap.html`.

- [ ] **Step 1: Write a failing runner test with synthetic factors and a stubbed period evaluator**

The test must assert that the runner resolves the frozen baseline, passes cutoff `2025-12-31`, emits every declared CSV/JSON/HTML artifact, never writes `frozen_challenger.json`, records zero 2026 hashes, and returns status `COMPLETE` even when no stable negative factor exists.

- [ ] **Step 2: Run the runner test and verify the module is missing**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_attribution_runner.py`

- [ ] **Step 3: Implement cached target evaluation and artifact writers**

Compute the causal factor frame once. Cache backtest results by a SHA-256 of the target-position bytes so identical counterfactuals do not repeat vectorbt work. Store long-form rows with `object_type`, `object_id`, `counterfactual`, `window`, champion metrics, counterfactual metrics, deltas, dominance share, and classification evidence.

- [ ] **Step 4: Implement all preregistered counterfactual families and heatmap**

Enumerate group coalitions, 12 raw signals, eligible primary states, six weight neighbors, four non-baseline threshold neighbors, and four non-baseline state-machine neighbors. Plot annual return and Sharpe deltas from `classification.csv`; embed Plotly for offline viewing.

- [ ] **Step 5: Run runner, factor, backtest, and no-lookahead tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_attribution_runner.py tests/test_factors.py tests/test_backtest.py tests/test_audit.py`

- [ ] **Step 6: Commit the runner**

```powershell
git add src/czsc_trader/attribution_runner.py tests/test_attribution_runner.py
git commit -m "research: implement champion attribution runner"
```

### Task 4: Integrate the Preregistered Experiment Entry Point

**Files:**
- Modify: `scripts/run_experiment.py`
- Modify: `tests/test_experiment_entrypoints.py`
- Modify during execution: `experiments/0824_EX02/03_execution.md`
- Modify during execution: `experiments/0824_EX02/04_conclusion.md`
- Regenerate during execution: `experiments/0824_EX02/experiment_manifest.json`

**Interfaces:**
- Consumes: `run_champion_attribution(...)` from Task 3.
- Produces: `run_preregistered_attribution(experiment_dir: Path) -> Path`.
- CLI: `.\.venv\Scripts\python.exe scripts\run_experiment.py --experiment-dir experiments\0824_EX02`.

- [ ] **Step 1: Write a failing entrypoint test for an existing PRE_REGISTERED archive**

The test must stub `run_champion_attribution`, finalize the same directory rather than allocate `EX03`, rewrite execution/conclusion from machine metrics, set manifest status `COMPLETE`, retain `holdout_accessed: false`, and reject any protocol whose cutoff exceeds `2025-12-31` or enables promotion.

- [ ] **Step 2: Run the focused entrypoint tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_experiment_entrypoints.py`

- [ ] **Step 3: Add argparse dispatch while preserving the existing EX01 API**

Keep `main(...)` compatible with existing tests. Add a separate finalization path selected only by `--experiment-dir`; it may write only inside the specified experiment directory, must preserve `artifacts/protocol.json`, and must rebuild and validate the manifest after all outputs and documents are final.

- [ ] **Step 4: Run entrypoint and archive tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_experiment_entrypoints.py tests/test_experiment_archive.py`

- [ ] **Step 5: Commit executable integration before seeing formal results**

```powershell
git add scripts/run_experiment.py tests/test_experiment_entrypoints.py
git commit -m "research: run preregistered attribution archives"
```

### Task 5: Execute, Audit, and Deliver EX02

**Files:**
- Populate: `experiments/0824_EX02/artifacts/`
- Modify: `experiments/0824_EX02/03_execution.md`
- Modify: `experiments/0824_EX02/04_conclusion.md`
- Regenerate: `experiments/0824_EX02/experiment_manifest.json`

**Interfaces:**
- Consumes: the committed preregistration and committed attribution implementation.
- Produces: a complete portable experiment archive with status `COMPLETE` or `ERROR`; never PASS/FAIL because this is diagnostic.

- [ ] **Step 1: Verify the preregistration and code commits precede result generation**

Run: `git log --oneline -- experiments/0824_EX02 src/czsc_trader/attribution.py src/czsc_trader/attribution_runner.py scripts/run_experiment.py`

- [ ] **Step 2: Run the formal experiment once**

Run: `.\.venv\Scripts\python.exe scripts\run_experiment.py --experiment-dir experiments\0824_EX02`

Expected: status `COMPLETE`, `holdout_accessed` false, no `frozen_challenger.json`, and all protocol-declared artifacts present.

- [ ] **Step 3: Inspect machine results before writing the narrative conclusion**

Read `classification.csv`, group/signal/state attribution, sensitivities, `metrics.json`, and the heatmap. The conclusion must report every stable-negative object, or explicitly state that none exists, and distinguish stable, mixed, state-dependent, redundant, insufficient, and unmodeled evidence.

- [ ] **Step 4: Run final focused and full verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src scripts dataflows
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

Also validate the experiment manifest, assert every recorded data hash is from 2020-2025, and assert the baseline file and registry still match their frozen hashes.

- [ ] **Step 5: Commit the immutable experiment result**

```powershell
git add experiments/0824_EX02
git commit -m "research: complete champion attribution experiment"
```

- [ ] **Step 6: Deliver evidence without merging or pushing**

Report the branch and commit, execution status, stable positive/negative factors, mixed or regime-dependent findings, most important rule/threshold sensitivities, 2026 access status, tests, and clickable paths to the conclusion and artifacts. Leave merge and push for an explicit user request.
