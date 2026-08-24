# Experiment Artifact Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store every formal research round in a tracked `experiments/MMDD_EXX/` archive with four required documents and complete machine evidence, while reserving `outputs/` for ordinary backtests only.

**Architecture:** Add a focused archive module that allocates date-scoped experiment numbers, builds a SHA-256 manifest, and validates an archive without understanding strategy metrics. Research scripts write their machine outputs into `artifacts/`, render the four research documents, and finalize the manifest; the ordinary backtest path remains unchanged.

**Tech Stack:** Python 3.12, pathlib, hashlib, JSON, pandas, pytest, Git.

**Spec:** `docs/superpowers/specs/2026-08-24-experiment-artifact-layout-design.md`

## Global Constraints

- `experiments/` is tracked by Git; `outputs/` remains ignored.
- Directory names are `MMDD_EXX`; `XX` resets to `01` each Asia/Shanghai date and increments from the highest existing same-date number.
- Existing directories are never overwritten and gaps are never filled.
- Each archive requires `01_goal.md`, `02_design.md`, `03_execution.md`, `04_conclusion.md`, `experiment_manifest.json`, and its declared machine artifacts.
- Manifests use relative paths, byte counts, and SHA-256; they contain no absolute path or `outputs/` revision.
- Research writes only under `experiments/`; ordinary backtests continue to write only under `outputs/<code>_<MMDD>_RXX/`.
- The current research verdict, champion rule, and 2026 holdout state must not change.
- No worktree and no subagent.

---

### Task 1: Implement experiment directory allocation and archive validation

**Files:**
- Create: `src/czsc_trader/experiment_archive.py`
- Create: `tests/test_experiment_archive.py`

**Interfaces:**
- Produces: `create_experiment_dir(root: Path, run_date: date) -> Path`
- Produces: `build_experiment_manifest(experiment_dir: Path, metadata: dict[str, object]) -> dict[str, object]`
- Produces: `validate_experiment_archive(experiment_dir: Path) -> dict[str, object]`
- Required documents: constant tuple `REQUIRED_DOCUMENTS`

- [x] **Step 1: Write failing allocation tests**

Add tests that assert an empty root creates `0824_EX01`, existing `EX01` and `EX03` create `EX04`, a new date resets to `EX01`, existing directories are not overwritten, and `EX99` raises `RuntimeError`.

```python
def test_experiment_number_resets_by_date_and_does_not_fill_gaps(tmp_path):
    (tmp_path / "0824_EX01").mkdir()
    (tmp_path / "0824_EX03").mkdir()
    assert create_experiment_dir(tmp_path, date(2026, 8, 24)).name == "0824_EX04"
    assert create_experiment_dir(tmp_path, date(2026, 8, 25)).name == "0825_EX01"
```

- [x] **Step 2: Run allocation tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_experiment_archive.py -q`

Expected: collection fails because `czsc_trader.experiment_archive` does not exist.

- [x] **Step 3: Implement the allocator**

Parse only names matching `rf"{run_date:%m%d}_EX(\\d{{2}})"`, choose `max(numbers, default=0) + 1`, reject values above 99, create the directory with `exist_ok=False`, and return it.

- [x] **Step 4: Add failing manifest and tamper tests**

Create all four documents and four current-experiment artifacts in a fixture. Assert the manifest excludes itself, contains relative paths, byte sizes, and SHA-256. Change one file after manifest generation and assert validation raises a hash mismatch. Remove each required document in a parametrized test and assert validation fails.

```python
manifest = build_experiment_manifest(archive, metadata)
assert "experiment_manifest.json" not in manifest["files"]
validate_experiment_archive(archive)
(archive / "04_conclusion.md").write_text("tampered", encoding="utf-8")
with pytest.raises(ValueError, match="SHA-256"):
    validate_experiment_archive(archive)
```

- [x] **Step 5: Implement manifest building and validation**

Walk regular files below the archive, exclude `experiment_manifest.json`, sort POSIX relative paths, and write schema version, metadata, size, and SHA-256. Validation checks the four documents, declared files, path containment, size, hash, and rejects absolute paths or any manifest string containing `outputs/` or an `_RXX` output reference.

- [x] **Step 6: Run archive tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_experiment_archive.py -q`

Expected: all tests pass.

### Task 2: Route research runs into tracked experiment archives

**Files:**
- Modify: `src/czsc_trader/experiments.py`
- Modify: `src/czsc_trader/research.py`
- Modify: `scripts/run_experiment.py`
- Modify: `scripts/run_holdout.py`
- Modify: `scripts/run_research.py`
- Modify: `tests/test_experiments.py`
- Create: `tests/test_experiment_entrypoints.py`

**Interfaces:**
- Consumes: `create_experiment_dir`, `build_experiment_manifest`, `validate_experiment_archive`
- Produces: research machine files below `<experiment_dir>/artifacts/`
- Produces: four Markdown documents and a finalized manifest for each completed research round

- [x] **Step 1: Write failing research-boundary tests**

Patch the experiment runner and assert `run_experiment.main()` creates an `MMDD_EXX` directory below a supplied experiments root, passes its `artifacts/` directory to the research engine, creates all four documents, and never references `outputs`. Change the legacy `run_dated_research` contract so its search artifacts also live below a date-scoped experiment archive. Keep `create_output_dir` and the ordinary backtest output-path tests unchanged as the regression contract.

- [x] **Step 2: Run entrypoint tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_experiment_entrypoints.py tests/test_output_paths.py -q`

Expected: research entrypoint lacks an experiments-root interface and still allocates an R-number output.

- [x] **Step 3: Preserve and consume the pre-2026 research result payload**

Keep candidate calculations and its return contract unchanged. Read champion metadata, visible cutoff, validation periods, and best-candidate metrics from the generated machine artifacts when rendering documents. Continue writing `candidate_results.csv`, `champion_metrics.json`, and `metrics.json` to the path supplied by the caller.

- [x] **Step 4: Implement archive rendering in `run_experiment.py`**

Add `main(experiments_root: Path = Path("experiments"), run_date: date | None = None) -> Path`. Allocate the experiment directory, copy the tracked protocol to `artifacts/protocol.json`, run the engine against `artifacts/`, render factual goal/design/execution/conclusion documents, build and validate the manifest, print the experiment directory, and return it. On failure, preserve the directory with an execution error record and do not reuse its number.

- [x] **Step 5: Route holdout research away from outputs**

Change `run_holdout.py` to allocate a new experiment archive, place holdout machine results under `artifacts/`, render the same four-document contract, and finalize a manifest. It must still require a frozen challenger from a tracked experiment archive and must not perform parameter search.

Update `scripts/run_research.py` and `run_dated_research` to allocate `MMDD_EXX`, write the legacy 23,760-candidate search below `artifacts/`, render the required four documents, and finalize the archive manifest. It must not call `create_output_dir`.

- [x] **Step 6: Run focused tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_experiments.py tests/test_experiment_entrypoints.py tests/test_output_paths.py -q`

Expected: all pass; ordinary backtest R-number behavior is unchanged.

### Task 3: Migrate the current research round into `0824_EX01`

**Files:**
- Create: `experiments/0824_EX01/01_goal.md`
- Create: `experiments/0824_EX01/02_design.md`
- Create: `experiments/0824_EX01/03_execution.md`
- Create: `experiments/0824_EX01/04_conclusion.md`
- Create: `experiments/0824_EX01/experiment_manifest.json`
- Create: `experiments/0824_EX01/artifacts/protocol.json`
- Create: `experiments/0824_EX01/artifacts/candidate_results.csv`
- Create: `experiments/0824_EX01/artifacts/champion_metrics.json`
- Create: `experiments/0824_EX01/artifacts/metrics.json`
- Delete: `configs/experiments/588080_reentry_v1.json`
- Delete: `docs/experiments/588080_reentry_v1_result.json`
- Modify: `README.md`
- Modify: `docs/RESEARCH_HANDOFF.md`
- Modify: `docs/superpowers/plans/2026-08-24-pre2026-champion-challenge.md`

**Interfaces:**
- Consumes: local research evidence from `outputs/588080_0824_R02` only during migration
- Produces: the sole tracked authoritative archive `experiments/0824_EX01`

- [x] **Step 1: Copy machine evidence and verify exact bytes**

Copy the protocol and three machine outputs into `artifacts/`. Compare source and destination SHA-256 before removing any source. Do not include a local output path in any tracked file.

- [x] **Step 2: Write the four factual research documents**

Record the approved goal and design, actual commands and environment, the Tushare row-cap failure and annual segmentation fix, `95 passed, 2 deselected`, zero visible 2026 hashes, all 20 candidates, the exact 2023–2025 champion/challenger table, overall FAIL, unchanged champion, no frozen challenger, and no holdout access.

- [x] **Step 3: Build and validate the archive manifest**

Use the archive module to create `experiment_manifest.json`. Run validation and independently assert the candidate CSV has 20 rows, the result status is FAIL, and no frozen challenger exists.

- [x] **Step 4: Remove duplicate authorities and update documentation**

Delete the old tracked protocol/result locations. Update README and the handoff document to point to `experiments/`, define the four-document contract, distinguish research from backtest outputs, and state that historical experiment archives—not local outputs—are the portable source of truth. Amend the old implementation plan so it names the migrated archive rather than a generated outputs directory.

- [x] **Step 5: Remove only the migrated research output directory**

Resolve and verify the absolute target is exactly `D:\CodeBase\czsc_trader\outputs\588080_0824_R02`, confirm the byte hashes match the archive, then remove that directory. Preserve `outputs/588080_0824_R01` and all other ordinary backtests.

- [x] **Step 6: Run migration-focused verification**

Run the archive validator, `rg -n "configs/experiments|docs/experiments|outputs/588080_0824_R02" README.md docs scripts src tests experiments`, and the focused tests. Expected: no active reference to the old authorities or research output; the archived plan may describe historical migration only if clearly labeled.

### Task 4: Final verification and commits

**Files:**
- Modify: `docs/superpowers/plans/2026-08-24-experiment-artifact-layout.md` (mark completed steps)

**Interfaces:**
- Produces: a clean research branch with auditable commits and no untracked changes

- [x] **Step 1: Run complete verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src scripts dataflows
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

Expected: full fast suite passes, compilation succeeds, dependencies are consistent, and no whitespace errors are reported.

- [x] **Step 2: Verify policy boundaries**

Assert `experiments/0824_EX01` is fully tracked, `.gitignore` still ignores `outputs/`, `outputs/588080_0824_R01` still exists, `outputs/588080_0824_R02` does not exist, baseline files have no diff, and 2026 holdout was not run.

- [x] **Step 3: Commit implementation and migrated archive**

Create focused commits for archive infrastructure and for the migrated experiment/documentation. Do not merge or push unless explicitly requested.
