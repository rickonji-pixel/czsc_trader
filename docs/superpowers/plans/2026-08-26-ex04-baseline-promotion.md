# EX04 Baseline Promotion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote the byte-identical `0824_EX04` frozen strategy to active, 588080-scoped `baseline_20260826` while retaining `baseline_20260823` only for explicit historical replay.

**Architecture:** Extend the immutable registry resolver to understand legacy fixed-rule and four-layer baselines, then route both through a small execution adapter returning the existing `AppliedRule` interface. Keep historical research callers stable by allowing explicit archived resolution, while the ordinary backtest passes its symbol and therefore enforces the new scope and default.

**Tech Stack:** Python 3.12, pandas, NumPy, pytest, existing CZSC factor generation and vectorbt backtest stack.

**Spec:** `docs/superpowers/specs/2026-08-26-ex04-baseline-promotion-design.md`

## Global Constraints

- `configs/rule_baselines/baseline_20260826.json` must be byte-identical to `experiments/0824_EX04/artifacts/frozen_challenger.json`.
- `baseline_20260823` remains immutable and becomes `archived`; it is never an implicit fallback.
- `baseline_20260826` is active only for `588080.SH`; other symbols must fail unless an archived version is explicitly requested for historical replay.
- The new baseline inherits only state-machine fields from the archived champion; factor identities, weights, and thresholds come from EX04.
- No 2026 performance snapshot, research run, holdout run, or output directory is created for promotion.
- The independent forward-validation start is `2026-08-26`; all data through `2026-08-24` is already observed.
- Work stays on `master`; no worktree or subagent is used.

---

### Task 1: Extend immutable baseline resolution and freeze the registry entry

**Files:**
- Modify: `tests/test_baselines.py`
- Modify: `src/czsc_trader/baselines.py`
- Create: `configs/rule_baselines/baseline_20260826.json`
- Modify: `configs/rule_baselines/registry.json`

**Interfaces:**
- Consumes: legacy fixed-rule JSON, EX04 frozen JSON, registry schema version 3.
- Produces: `resolve_baseline(root: Path, version: str | None = None, *, symbol: str | None = None) -> ResolvedBaseline` with `strategy`, `status`, `scope`, `symbol`, `factor_names`, `factor_weights`, lineage, selection cutoff, and forward start.

- [ ] **Step 1: Write failing resolver tests**

Add tests that construct a temporary repository layout and assert:

```python
resolved = resolve_baseline(root, symbol="588080.SH")
assert resolved.version == "baseline_20260826"
assert resolved.strategy == "czsc_four_layer"
assert resolved.status == "active"
assert resolved.symbol == "588080.SH"
assert resolved.factor_names == tuple(frozen["factor_names"])
assert resolved.rule.enter == 0.175
assert resolved.rule.exit == 0.025
assert sum(abs(value) for value in resolved.factor_weights) == pytest.approx(1.0)
```

Also assert that an implicit archived latest is rejected, the active baseline rejects `600519.SH`, explicit `baseline_20260823` resolves with `status == "archived"`, a modified promoted copy fails canonical hash validation, a modified source fails source hash validation, and a champion hash mismatch fails state-machine resolution.

- [ ] **Step 2: Run resolver tests and verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/test_baselines.py -q`

Expected: failures because `ResolvedBaseline` lacks four-layer fields and `resolve_baseline` lacks symbol-aware behavior.

- [ ] **Step 3: Implement the schema-3 resolver**

Extend `ResolvedBaseline` with immutable metadata and optional factor tuples. Keep `_parse_rule` unchanged for `czsc_fixed_rule`. Add `_parse_four_layer(payload, archived_rule)` that validates:

```python
factor_names = tuple(map(str, payload["factor_names"]))
factor_weights = tuple(float(payload["weights"][name]) for name in factor_names)
if len(factor_names) != 12 or len(set(factor_names)) != 12:
    raise ValueError("four-layer baseline must freeze 12 unique factors")
if abs(sum(map(abs, factor_weights)) - 1.0) > 1e-12:
    raise ValueError("four-layer baseline weights must have L1 norm one")
```

Build a `Rule` with archived group weights and state-machine fields but EX04 `spec.enter/spec.exit`. Verify `champion.version`, `champion.sha256`, source byte hash, promoted/source byte equality, active scope, and explicit archived access.

- [ ] **Step 4: Freeze the configuration bytes and registry**

Use a byte-preserving copy of the tracked EX04 artifact for `baseline_20260826.json`. Set registry `schema_version` to 3, `latest` to `baseline_20260826`, add active symbol-scoped lineage fields, and mark the old entry archived without changing its existing identity fields.

- [ ] **Step 5: Run resolver tests and verify GREEN**

Run: `.venv/Scripts/python.exe -m pytest tests/test_baselines.py -q`

Expected: all baseline resolver tests pass.

- [ ] **Step 6: Commit registry and resolver**

Run:

```powershell
git add tests/test_baselines.py src/czsc_trader/baselines.py configs/rule_baselines
git commit -m "feat: promote EX04 to active 588080 baseline"
```

### Task 2: Add a unified frozen-baseline execution adapter

**Files:**
- Create: `tests/test_baseline_execution.py`
- Create: `src/czsc_trader/baseline_execution.py`
- Modify: `src/czsc_trader/four_layer.py`

**Interfaces:**
- Consumes: full generated factor frame and `ResolvedBaseline`.
- Produces: `apply_resolved_baseline(factor_frame: pd.DataFrame, baseline: ResolvedBaseline) -> AppliedRule` and `build_four_layer_events(target_position, scores, enter, exit_) -> pd.DataFrame`.

- [ ] **Step 1: Write failing adapter tests**

Create a small four-layer resolved fixture and numeric raw-factor frame. Assert the adapter selects factors in frozen order, produces the direct `score_four_layer` result, evolves positions with the inherited state machine, and emits deterministic `FourLayer:YYYYMMDD:Entry/Exit` events. Add failure tests for missing and duplicated frozen factor identities. Add a legacy fixture proving fixed-rule dispatch still equals `apply_fixed_rule`.

- [ ] **Step 2: Run adapter tests and verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/test_baseline_execution.py -q`

Expected: collection failure because `czsc_trader.baseline_execution` does not exist.

- [ ] **Step 3: Implement event construction**

Move the stable event shape into public `build_four_layer_events`:

```python
previous = target_position.shift(1, fill_value=0.0)
for signal_date in target_position.index[target_position.ne(previous)]:
    event_type = "Entry" if target_position.loc[signal_date] == 1.0 else "Exit"
    event_id = f"FourLayer:{pd.Timestamp(signal_date):%Y%m%d}:{event_type}"
```

Include score, enter/exit thresholds, and before/after positions so the existing audit can validate next-day execution.

- [ ] **Step 4: Implement strategy dispatch**

For `czsc_fixed_rule`, call `apply_fixed_rule`. For `czsc_four_layer`, normalize the generated raw columns, require every frozen factor exactly once, select the frozen order, validate weights, score, evolve positions, build events, and return `AppliedRule`. Reject unknown strategy values.

- [ ] **Step 5: Run adapter and four-layer tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_baseline_execution.py tests/test_four_layer.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Commit the execution adapter**

Run:

```powershell
git add tests/test_baseline_execution.py src/czsc_trader/baseline_execution.py src/czsc_trader/four_layer.py
git commit -m "feat: execute frozen four-layer baselines"
```

### Task 3: Make ordinary backtests use the active scoped baseline

**Files:**
- Modify: `tests/test_backtest_runner.py`
- Modify: `src/czsc_trader/backtest_runner.py`

**Interfaces:**
- Consumes: symbol-aware `resolve_baseline` and `apply_resolved_baseline`.
- Produces: default 588080 ordinary backtests using `baseline_20260826`, enriched manifest lineage, and explicit failures for scope misuse.

- [ ] **Step 1: Write failing backtest integration tests**

Update the existing default test to expect:

```python
assert manifest["baseline"]["version"] == "baseline_20260826"
assert manifest["baseline"]["strategy"] == "czsc_four_layer"
assert manifest["baseline"]["status"] == "active"
assert manifest["baseline"]["symbol"] == "588080.SH"
assert manifest["baseline"]["source_path"] == "experiments/0824_EX04/artifacts/frozen_challenger.json"
assert manifest["baseline"]["forward_validation_start"] == "2026-08-26"
```

Add tests that another symbol without a version and another symbol explicitly requesting `baseline_20260826` both produce a failure record, while explicit `baseline_20260823` remains replayable and is marked archived.

- [ ] **Step 2: Run backtest tests and verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/test_backtest_runner.py -q`

Expected: failures because the runner still applies only fixed rules and does not pass the symbol to resolution.

- [ ] **Step 3: Integrate the adapter and manifest metadata**

Change baseline resolution to:

```python
baseline = resolve_baseline(request.baseline_root, request.baseline, symbol=request.symbol)
applied = apply_resolved_baseline(factor_result.frame, baseline)
```

Keep output filenames stable. Add strategy/status/scope/symbol/source/selection/forward fields to the manifest, and write the full frozen payload to `baseline_rule.json`.

- [ ] **Step 4: Prove promoted bytes and execution identity**

Add a focused repository test that compares `baseline_20260826.json` bytes with the EX04 source, resolves the default for `588080.SH`, generates the tracked factor frame through the existing data loader, and compares adapter scores/positions with direct EX04 `normalized_signal_factors` + `score_four_layer` + `positions_from_scores` execution.

- [ ] **Step 5: Run backtest and identity regressions**

Run: `.venv/Scripts/python.exe -m pytest tests/test_backtest_runner.py tests/test_baseline_snapshot.py tests/test_baselines.py tests/test_baseline_execution.py -q`

Expected: all selected tests pass without creating tracked outputs.

- [ ] **Step 6: Commit ordinary backtest integration**

Run:

```powershell
git add tests/test_backtest_runner.py tests/test_baseline_snapshot.py src/czsc_trader/backtest_runner.py
git commit -m "feat: default 588080 backtests to EX04 baseline"
```

### Task 4: Update handoff semantics and verify the promotion

**Files:**
- Modify: `docs/RESEARCH_HANDOFF.md`
- Modify: `docs/superpowers/plans/2026-08-26-ex04-baseline-promotion.md`

**Interfaces:**
- Produces: cross-device documentation that identifies the active/archived baselines and the new forward-only validation boundary.

- [ ] **Step 1: Update the handoff**

Record `baseline_20260826` as the active 588080 baseline sourced from 0824_EX04; mark `baseline_20260823` historical-only; add 0826_EX01 `no_stable_boundary`; replace the old per-experiment 2026 holdout guidance with the fact that 2026 is program-level observed and future independent evidence begins on 2026-08-26.

- [ ] **Step 2: Run focused and archive verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_baselines.py tests/test_baseline_execution.py tests/test_backtest_runner.py tests/test_baseline_snapshot.py -q
.\.venv\Scripts\python.exe -c "from pathlib import Path; from czsc_trader.experiment_archive import validate_experiment_archive; [validate_experiment_archive(p) for p in sorted(Path('experiments').iterdir()) if p.is_dir()]; print('experiment archives: PASS')"
.\.venv\Scripts\python.exe -m compileall -q src tests scripts
git diff --check
```

Expected: all focused tests, all experiment archive checks, compilation, and whitespace validation pass.

- [ ] **Step 3: Run the complete test suite**

Run: `.venv/Scripts/python.exe -m pytest -q`

Expected: all tests pass; existing Optuna experimental warnings may remain unchanged.

- [ ] **Step 4: Commit documentation and final verification state**

Run:

```powershell
git add docs/RESEARCH_HANDOFF.md docs/superpowers/plans/2026-08-26-ex04-baseline-promotion.md
git commit -m "docs: hand off active EX04 baseline"
git status --short --branch
```

Expected: `master` is clean and ahead of `origin/master` only by the promotion commits; pushing is not performed unless explicitly requested.
