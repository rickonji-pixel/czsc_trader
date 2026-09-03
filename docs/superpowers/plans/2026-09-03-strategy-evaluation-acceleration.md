# Strategy Evaluation Acceleration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce a cold 1,187-candidate screening run from about 24 minutes to at most 5 minutes while preserving exact evaluation semantics, and reuse explicitly declared immutable experiment evidence in at most 1 minute when every required observation is available.

**Architecture:** Keep the existing evaluator as the reference path. Add a prepared read-only workspace, exact block scoring, global realized-behavior deduplication, and process-parallel execution of unique behavior groups. Before computing, resolve only explicitly declared completed experiment artifacts by a content identity; the current experiment still publishes a complete result and a row-level reuse ledger.

**Tech Stack:** Python 3.12, pandas 3, NumPy 2.4, vectorbt 1.1, joblib 1.5/loky, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-03-parallel-strategy-evaluation-design.md`

## Global Constraints

- Do not change OPC-v1, OPC-v2, candidate payloads, signals, execution rules, fees, windows, metrics, ranking, or promotion semantics.
- `MetricObservation.to_dict()` and canonical metric order must be identical across reference, optimized, serial, parallel, and reused paths.
- Historical experiments are read-only; reuse is limited to `reuse_source_experiments` declared by the current candidate manifest.
- Missing or unverifiable evidence is a cache miss and must be recomputed; corrupted declared evidence must be recorded diagnostically.
- Child processes never write research files.
- `evaluation_workers` defaults to `1`; `reuse_experiment_artifacts` defaults to `false`; `reuse_source_experiments` defaults to an empty list.
- Keep `METRIC_SEMANTICS_VERSION = "candidate-metrics-v1"` unchanged unless metric meaning changes.
- Cold 1,187-candidate screening must complete in at most 300 seconds; a fully reused evaluation must complete in at most 60 seconds.
- Relative to the existing serial reference path, at least one combined algorithmic and multi-process configuration must reach 3.0x on the fixed benchmark.

---

### Task 1: Freeze execution configuration and metric identity

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/czsc_trader/candidate_evaluation.py`
- Modify: `src/czsc_trader/application/evaluation_service.py`
- Modify: `tests/test_candidate_evaluation.py`
- Modify: `tests/test_evaluation_service.py`

**Interfaces:**
- Produces: `METRIC_SEMANTICS_VERSION: str`.
- Produces: `CandidateEvaluationContext.workers: int` with default `1`.
- Produces: validated manifest settings `evaluation_workers`, `reuse_experiment_artifacts`, and `reuse_source_experiments`.

- [ ] **Step 1: Write failing configuration tests**

Add tests that construct manifests with missing settings, valid settings, zero workers, reuse enabled without sources, and sources present while reuse is disabled:

```python
def test_candidate_context_defaults_to_one_worker():
    dates = pd.date_range("2026-01-01", periods=2, freq="D")
    context = CandidateEvaluationContext(
        SimpleNamespace(raw_dir=Path("raw"), baseline_root=Path("baselines")),
        "588080.SH",
        "etf",
        (("full", (dates[0], dates[1])),),
    )
    assert context.workers == 1


def test_evaluation_rejects_nonpositive_workers(tmp_path):
    experiment = write_bundle(tmp_path)
    manifest_path = experiment / "candidate_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["evaluation_workers"] = 0
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="evaluation_workers"):
        evaluate_experiment(RepositoryContext.discover(tmp_path, explicit_root=tmp_path), "0903_TEST", runner=fake_runner)


def test_reuse_requires_explicit_sources(tmp_path):
    experiment = write_bundle(tmp_path)
    manifest_path = experiment / "candidate_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["reuse_experiment_artifacts"] = True
    manifest["reuse_source_experiments"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="reuse_source_experiments"):
        evaluate_experiment(RepositoryContext.discover(tmp_path, explicit_root=tmp_path), "0903_TEST", runner=fake_runner)
```

Add `from pathlib import Path` to `tests/test_candidate_evaluation.py` and `import pytest` to
`tests/test_evaluation_service.py`; the other names already exist in the two test modules.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `pytest tests/test_candidate_evaluation.py tests/test_evaluation_service.py -q`

Expected: failures for the missing `workers` field and absent validation.

- [ ] **Step 3: Add dependency, constants, and strict parsing**

Add `"joblib>=1.5,<2"` to project dependencies. Add:

```python
METRIC_SEMANTICS_VERSION = "candidate-metrics-v1"

@dataclass(frozen=True)
class CandidateEvaluationContext:
    repository: Any
    symbol: str
    asset_type: str
    periods: tuple[tuple[str, tuple[pd.Timestamp, pd.Timestamp]], ...]
    fee_rate: float = 0.0005
    init_cash: float = 1_000_000.0
    workers: int = 1
```

In `evaluation_service.py`, parse settings before constructing the context. Require a positive integer, a Boolean reuse flag, a list of direct-child experiment names, and at least one source when reuse is enabled. Pass `workers` into the context.

- [ ] **Step 4: Run focused tests**

Run: `pytest tests/test_candidate_evaluation.py tests/test_evaluation_service.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml src/czsc_trader/candidate_evaluation.py src/czsc_trader/application/evaluation_service.py tests/test_candidate_evaluation.py tests/test_evaluation_service.py
git commit -m "feat: validate evaluation execution settings"
```

### Task 2: Add exact historical experiment evidence reuse

**Files:**
- Create: `src/czsc_trader/evaluation_artifacts.py`
- Create: `tests/test_evaluation_artifacts.py`
- Modify: `src/czsc_trader/application/evaluation_service.py`
- Modify: `tests/test_evaluation_service.py`

**Interfaces:**
- Produces: `EvaluationIdentity`, whose `key` is a canonical SHA-256 digest.
- Produces: `ReuseLedgerRow` with source experiment, source file hash, and `REUSED` or `COMPUTED` status.
- Produces: `load_reusable_observations(experiments_root, source_ids, requested) -> ReuseResult`.
- Consumes: `METRIC_SEMANTICS_VERSION` from Task 1.

- [ ] **Step 1: Write identity and integrity tests**

Cover exact-key matches, strategy/data/window/scenario/version mismatches, undeclared sources, incomplete experiments, source artifact hash mismatches, partial hits, and CSV-to-`MetricObservation` reconstruction:

```python
def identity(**changes):
    values = {
        "candidate_id": "c1",
        "candidate_hash": "c" * 64,
        "execution_policy_hash": "a" * 64,
        "data_identity": "d" * 64,
        "development_cutoff": "2026-09-02",
        "window_id": "full",
        "window_start": "2021-01-04",
        "window_end": "2026-09-02",
        "tier": "SCREENING",
        "scenario_id": "standard",
        "fee_rate": 0.0005,
        "metric_semantics_version": "candidate-metrics-v1",
    }
    values.update(changes)
    return EvaluationIdentity(**values)


def test_evaluation_identity_changes_when_execution_changes():
    left = identity(execution_policy_hash="a" * 64)
    right = identity(execution_policy_hash="b" * 64)
    assert left.key != right.key


def test_loader_returns_only_exact_verified_hits(tmp_path):
    source = write_completed_source(tmp_path)
    requested = (identity(candidate_id="c1"), identity(candidate_id="c2"))
    result = load_reusable_observations(tmp_path / "experiments", (source.name,), requested)
    assert [x.candidate_id for x in result.observations] == ["c1"]
    assert result.missing_keys == (requested[1].key,)
    assert result.ledger[0].status == "REUSED"
```

- [ ] **Step 2: Run the new test file and confirm failure**

Run: `pytest tests/test_evaluation_artifacts.py -q`

Expected: import failure for the new module.

- [ ] **Step 3: Implement canonical identities and source validation**

Use immutable dataclasses. The identity payload must contain:

```python
{
    "metric_semantics_version": METRIC_SEMANTICS_VERSION,
    "data_identity": canonical_json_sha256(manifest["source_files"]),
    "development_cutoff": protocol.development_cutoff.isoformat(),
    "candidate_hash": candidate_hash,
    "execution_policy_hash": execution_policy_hash,
    "window_id": window_id,
    "window_start": start.date().isoformat(),
    "window_end": end.date().isoformat(),
    "tier": tier,
    "scenario_id": scenario_id,
    "fee_rate": fee_rate,
}
```

For each declared source, require `experiment_manifest.json` status `COMPLETE`; verify the manifest-recorded hashes for its protocol, candidate manifest, `evaluation_result.json`, and metric CSV before reading rows. Treat old sources with no explicit metric version as `candidate-metrics-v1` only after all archive hashes validate.

- [ ] **Step 4: Integrate partial reuse into the orchestration call boundary**

Add a small resolver in `evaluate_experiment` that builds requested identities per tier, passes only misses to the runner, restores canonical candidate/window/scenario order, and accumulates reuse ledger rows. Keep reuse disabled for the reproducibility rerun used by `_health`, because that call is intended to prove repeated computation.

- [ ] **Step 5: Run reuse and service tests**

Run: `pytest tests/test_evaluation_artifacts.py tests/test_evaluation_service.py -q`

Expected: PASS, including a partial-hit test where the runner receives only the missing candidate.

- [ ] **Step 6: Commit**

```powershell
git add src/czsc_trader/evaluation_artifacts.py src/czsc_trader/application/evaluation_service.py tests/test_evaluation_artifacts.py tests/test_evaluation_service.py
git commit -m "feat: reuse verified experiment evaluation evidence"
```

### Task 3: Prepare immutable workspace and exact block scoring

**Files:**
- Modify: `src/czsc_trader/candidate_evaluation.py`
- Modify: `tests/test_candidate_evaluation.py`

**Interfaces:**
- Produces: `EvaluationWorkspace` holding loaded market data, normalized factor arrays, aligned close, and period indexes.
- Produces: `PreparedCandidate` holding candidate identity, resolved baseline, score, target, events, and optional regimes.
- Produces: `EvaluationBatchResult` holding observations, named phase seconds, candidate count, and unique behavior count.
- Produces: `_prepare_candidates(workspace, selected) -> tuple[PreparedCandidate, ...]`.
- Produces: `evaluate_candidate_payloads_profiled(...) -> EvaluationBatchResult` for the benchmark only.
- Preserves: `_evaluate_candidate_payloads_reference(...)` as the correctness oracle.

- [ ] **Step 1: Write parity and load-count tests**

Use three candidates: ordinary values, values exactly on enter/exit thresholds, and two different regime parameter sets. Assert target arrays, score arrays, events, and final observation dictionaries equal the reference path. Assert market data, factors, normalization, and each distinct regime classification are built once.

```python
optimized = evaluate_candidate_payloads(context, protocol, payloads, ids, "SCREENING")
reference = _evaluate_candidate_payloads_reference(context, protocol, payloads, ids, "SCREENING")
assert [x.to_dict() for x in optimized] == [x.to_dict() for x in reference]
```

- [ ] **Step 2: Run parity tests and confirm failure**

Run: `pytest tests/test_candidate_evaluation.py -q`

Expected: failures because the workspace and reference entry point do not exist.

- [ ] **Step 3: Extract the current implementation as the reference path**

Move the current function body without semantic changes into `_evaluate_candidate_payloads_reference`. Keep `evaluate_candidate_payloads` delegating to it until the optimized path passes all parity tests.

- [ ] **Step 4: Implement workspace preparation and block scoring**

Construct prices, normalized factors, close alignment, and period indexes once. Score candidate blocks using the same structure/trend/volume group order as `score_four_layer`. Compare targets against the reference for threshold-boundary fixtures; route a candidate through `score_four_layer` whenever block scoring cannot prove the same target at every date.

- [ ] **Step 5: Add phase timing without changing the public runner result**

Return an `EvaluationBatchResult` from `evaluate_candidate_payloads_profiled` with seconds for `workspace`, `score_and_position`, `transaction`, `candidate_audit`, and `assemble`. Keep `evaluate_candidate_payloads` as a compatibility wrapper that returns only `.observations`.

- [ ] **Step 6: Remove per-candidate frame copying from the optimized path**

Build candidate-specific provenance frames only when attaching actual order events. Reuse immutable factor columns and add score/target views for the dates referenced by orders rather than copying the full frame for every candidate.

- [ ] **Step 7: Run tests**

Run: `pytest tests/test_candidate_evaluation.py tests/test_backtest.py tests/test_audit.py -q`

Expected: PASS with exact reference parity.

- [ ] **Step 8: Commit**

```powershell
git add src/czsc_trader/candidate_evaluation.py tests/test_candidate_evaluation.py
git commit -m "perf: prepare and batch candidate signals"
```

### Task 4: Deduplicate realized trading behavior globally

**Files:**
- Modify: `src/czsc_trader/candidate_evaluation.py`
- Modify: `tests/test_candidate_evaluation.py`

**Interfaces:**
- Produces: `behavior_key(prepared, window, scenario, fee_rate) -> str`.
- Produces: `_group_behaviors(prepared, windows, scenarios, fee_rate) -> tuple[BehaviorGroup, ...]`.
- Consumes: `PreparedCandidate` and `EvaluationWorkspace` from Task 3.

- [ ] **Step 1: Write behavior-sharing tests**

Create two candidates with different hashes and scores but identical target positions, plus a third with a different target. Monkeypatch the transaction simulation and assert two calls rather than three. Assert every candidate still receives its own observation and its own audit invocation.

```python
assert transaction_calls == 2
assert audit_calls == {"c1": 1, "c2": 1, "c3": 1}
assert [row.candidate_id for row in observations] == ["c1", "c2", "c3"]
```

Also prove that changing execution-policy hash, fee scenario, window, or target bytes prevents sharing.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `pytest tests/test_candidate_evaluation.py -q`

Expected: transaction call count remains three.

- [ ] **Step 3: Implement canonical realized-behavior grouping**

Hash the contiguous float target bytes together with execution-policy hash, window dates, scenario, and effective fee. Group only after all candidate targets are available, so duplicates crossing future process boundaries are found.

- [ ] **Step 4: Share transaction results and preserve candidate evidence**

Run daily or complete-execution simulation once per behavior group. For each member candidate, build its own factor events, run `audit_no_lookahead`, compute regime objectives from its regime labels, and create its own `MetricObservation`.

- [ ] **Step 5: Run parity and call-count tests**

Run: `pytest tests/test_candidate_evaluation.py -q`

Expected: PASS; observation dictionaries equal the reference implementation.

- [ ] **Step 6: Commit**

```powershell
git add src/czsc_trader/candidate_evaluation.py tests/test_candidate_evaluation.py
git commit -m "perf: deduplicate realized candidate behavior"
```

### Task 5: Parallelize unique behavior blocks safely

**Files:**
- Modify: `src/czsc_trader/candidate_evaluation.py`
- Modify: `tests/test_candidate_evaluation.py`

**Interfaces:**
- Produces: `_contiguous_chunks(items, workers) -> tuple[tuple[BehaviorGroup, ...], ...]`.
- Produces: `_evaluate_behavior_chunk(workspace, chunk) -> tuple[BehaviorResult, ...]`.
- Consumes: `CandidateEvaluationContext.workers` and global behavior groups from Tasks 1 and 4.

- [ ] **Step 1: Write chunking, ordering, and failure tests**

Cover 0, 1, fewer-than-32, non-divisible, and workers-greater-than-items inputs. Assert canonical order after deliberately delayed child results. Assert a child exception is raised to the caller and no output file is created.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `pytest tests/test_candidate_evaluation.py -q`

Expected: missing parallel helpers.

- [ ] **Step 3: Implement loky execution**

Use `joblib.Parallel` with the loky backend, `inner_max_num_threads=1`, `max_nbytes="1M"`, and `mmap_mode="r"`. Use the serial optimized path for `workers == 1` or fewer than 32 unique behavior groups. Return results only; keep all writes in the parent.

- [ ] **Step 4: Restore canonical ordering and expose optimized entry point**

Merge child results by behavior key, expand them to candidate observations, and order by requested candidate ID, scenario order, and period order. Switch `evaluate_candidate_payloads` from the reference delegate to the optimized implementation.

- [ ] **Step 5: Run exact serial/parallel/reference tests**

Run: `pytest tests/test_candidate_evaluation.py -q`

Expected: PASS for workers `1`, `2`, `4`, and `8`, with identical `to_dict()` sequences.

- [ ] **Step 6: Commit**

```powershell
git add src/czsc_trader/candidate_evaluation.py tests/test_candidate_evaluation.py
git commit -m "perf: parallelize unique evaluation behaviors"
```

### Task 6: Publish complete reuse and performance audit artifacts

**Files:**
- Modify: `src/czsc_trader/application/evaluation_service.py`
- Modify: `tests/test_evaluation_service.py`
- Modify: `src/czsc_trader/experiment_archive.py`
- Modify: `tests/test_identity_and_archives.py`

**Interfaces:**
- Produces: `artifacts/artifact_reuse.csv` for every requested observation.
- Produces: canonical metric hash in `evaluation_result.json`.
- Consumes: reuse ledger rows and computed observations from Tasks 2 and 5.

- [ ] **Step 1: Write atomic-publication and completed-result validation tests**

Assert `artifact_reuse.csv` contains one row per requested identity, including `evaluation_key`, status, source experiment, source file, and source hash. Delete or mutate the file after completion and assert re-opening the evaluation fails integrity validation.

- [ ] **Step 2: Run service tests and confirm failure**

Run: `pytest tests/test_evaluation_service.py tests/test_identity_and_archives.py -q`

Expected: missing reuse artifact and hash validation failures.

- [ ] **Step 3: Publish the reuse ledger atomically**

Add `artifact_reuse.csv` to the same temporary-directory commit sequence as existing artifacts. Include its normalized text hash and the canonical hash of ordered observation dictionaries in `evaluation_result.json`. Validate both when returning an already completed experiment.

- [ ] **Step 4: Cover the new artifact in archive validation**

Require the file for evaluations that enable reuse and verify its archive-manifest hash without changing historical archive requirements.

- [ ] **Step 5: Run service and archive tests**

Run: `pytest tests/test_evaluation_service.py tests/test_identity_and_archives.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add src/czsc_trader/application/evaluation_service.py src/czsc_trader/experiment_archive.py tests/test_evaluation_service.py tests/test_identity_and_archives.py
git commit -m "feat: audit reused evaluation evidence"
```

### Task 7: Benchmark, tune, and resume EX05 only after the gates pass

**Files:**
- Create: `experiments/0903_EX05/benchmark_evaluation.py`
- Modify: `experiments/0903_EX05/run_experiment.py`
- Modify: `experiments/0903_EX05/02_design.md`
- Modify: `docs/DEVELOPMENT_HANDOFF.md`
- Test: `tests/test_opc_v2_revaluation.py`

**Interfaces:**
- Produces: `experiments/0903_EX05/artifacts/evaluation_benchmark.json`.
- Produces: a regenerated immutable EX05 candidate manifest containing the selected worker count and explicit source `0903_EX04`.
- Consumes: all optimized and reuse interfaces from Tasks 1–6.

- [ ] **Step 1: Extend EX05 preregistration tests**

Assert manifest generation preserves all 1,187 payloads while adding:

```python
assert manifest["evaluation_workers"] in {2, 4, 8}
assert manifest["reuse_experiment_artifacts"] is True
assert manifest["reuse_source_experiments"] == ["0903_EX04"]
assert manifest["metric_semantics_version"] == "candidate-metrics-v1"
```

- [ ] **Step 2: Implement the fixed benchmark**

Select the 64 non-incumbents at indices `floor(k * (N - 1) / 63)` after sorting IDs. Measure the reference path, optimized one-process path, and optimized `2`, `4`, and `8` process paths with reuse disabled. Write CPU identity, Python/joblib versions, candidate IDs, phase times, speedups, unique behavior count, exact-equivalence result, and selected worker count to JSON.

- [ ] **Step 3: Run the benchmark and apply the selection rule**

Run: `.\.venv\Scripts\python.exe experiments\0903_EX05\benchmark_evaluation.py`

Expected: all optimized outputs exactly equal the reference; at least one multi-process configuration reaches 3.0x end-to-end relative to the existing serial reference. Report the additional speedup relative to optimized one-process for diagnosis. Choose the fastest valid configuration, preferring fewer workers when elapsed times differ by less than 5%.

- [ ] **Step 4: Run the cold full-scale gate**

Run the full SCREENING tier with reuse disabled and the selected worker count.

Expected: at most 300 seconds for 1,187 candidates. If it exceeds 300 seconds, keep EX05 paused, use recorded phase timings to optimize the dominant phase, and repeat Steps 3–4 before continuing.

- [ ] **Step 5: Run the fully reused gate**

Run an evaluation fixture whose every requested observation is present in a declared, hash-valid source experiment.

Expected: at most 60 seconds, zero runner candidates, and identical canonical metric hash.

- [ ] **Step 6: Regenerate the EX05 manifest and run focused verification**

Run:

```powershell
.\.venv\Scripts\python.exe experiments\0903_EX05\run_experiment.py
pytest tests/test_candidate_evaluation.py tests/test_evaluation_artifacts.py tests/test_evaluation_service.py tests/test_opc_v2_revaluation.py -q
ruff check src/czsc_trader tests experiments/0903_EX05
python -m compileall -q src packages experiments/0903_EX05
```

Expected: EX05 completes with full screening audit, reuse ledger, benchmark evidence, and all checks PASS.

- [ ] **Step 7: Update development handoff and commit**

Document configuration defaults, explicit artifact sources, cold/partial/full reuse meanings, benchmark command, selected worker count, measured timings, and failure diagnostics.

```powershell
git add experiments/0903_EX05 docs/DEVELOPMENT_HANDOFF.md tests/test_opc_v2_revaluation.py
git commit -m "research: complete accelerated OPC-v2 revaluation"
```

### Task 8: Final verification and delivery review

**Files:**
- Review: all files changed by Tasks 1–7

**Interfaces:**
- Consumes: completed implementation and EX05 evidence.
- Produces: verified branch ready for user-authorized merge or push.

- [ ] **Step 1: Run the minimal complete test suite**

Run:

```powershell
pytest -q
ruff check .
python -m compileall -q src packages experiments/0903_EX05
```

Expected: all commands exit `0`.

- [ ] **Step 2: Verify repository and research invariants**

Confirm `git diff --check` passes; EX04 files are unchanged; EX05 uses all 1,187 original candidate payloads; no file under `state` is tracked; every reused observation has a verified source row; and the reproducibility health rerun reports `COMPUTED`.

- [ ] **Step 3: Review the branch diff**

Inspect `git diff master...HEAD --stat` and `git diff master...HEAD`. Reject unrelated changes and confirm the untracked pre-plan EX05 manifest is regenerated only by the approved runner.

- [ ] **Step 4: Commit any verification-only documentation correction**

If measured evidence requires a documentation correction, edit only the affected handoff or EX05 execution note, rerun `git diff --check`, and commit:

```powershell
git add docs/DEVELOPMENT_HANDOFF.md experiments/0903_EX05
git commit -m "docs: record evaluation acceleration verification"
```

- [ ] **Step 5: Stop before integration**

Report measured cold, partial-reuse, and full-reuse times; selected worker count; exact-equivalence evidence; EX05 conclusion; commits; and remaining untracked files. Wait for explicit user authorization before merging to `master` or pushing the remote.
