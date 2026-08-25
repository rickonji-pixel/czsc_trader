# 0825_EX05 Dominant Exit Path Anatomy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Diagnose whether the three dominant `0825_EX04` false exits share a causal, low-contamination trajectory signature in the twelve signals of the `0824_EX04` frozen strategy.

**Architecture:** A dedicated runner validates the tracked source chain, regenerates the frozen causal factor frame through 2025, extracts a fixed pre-signal trajectory and descriptor matrix for all twenty events, and applies a deterministic discovery-confirmation classification. The stable experiment entrypoint finalizes the same preregistered directory and never creates a candidate or accesses holdout data.

**Tech Stack:** Python 3.12, pandas, NumPy, CZSC 1.0.1, pytest, Git-tracked experiment archives.

**Spec:** `experiments/0825_EX05/01_goal.md` and `experiments/0825_EX05/02_design.md`

## Global Constraints

- Research baseline is `0824_EX04`; event diagnosis source is `0825_EX04`.
- Only the twelve frozen `0824_EX04` raw CZSC signals and dates `T-20…T` are machine inputs.
- No 2026 data, optimization, classifier, feature combination, candidate, order, or frozen challenger.
- Formal execution occurs once after the implementation commit.
- Work in the current branch without worktrees or subagents.

---

### Task 1: Atomic descriptor and classification core

**Files:**
- Create: `tests/test_dominant_exit_anatomy_runner.py`
- Create: `src/czsc_trader/dominant_exit_anatomy_runner.py`

**Interfaces:**
- Produces: `validate_protocol(protocol) -> None`
- Produces: `extract_signal_descriptors(raw, mapped, contributions, score, signal_date, protocol) -> dict[str, object]`
- Produces: `discover_atomic_signatures(matrix, events, protocol) -> tuple[pd.DataFrame, pd.DataFrame]`
- Produces: `classify_anatomy(events, discovered, confirmed, protocol) -> dict[str, object]`

- [ ] Write literal tests for run-length bins, transition age, flip bins, contribution signs, score lags, no-future slicing, constant-signature removal, sequential confirmation, protective contamination, and all five classification branches.
- [ ] Run `.\.venv\Scripts\python.exe -m pytest -q tests/test_dominant_exit_anatomy_runner.py` and verify RED because the module is absent.
- [ ] Implement the smallest pure functions that satisfy the frozen descriptors and classification order.
- [ ] Run the focused tests and verify GREEN.

### Task 2: Formal causal runner and artifacts

**Files:**
- Modify: `tests/test_dominant_exit_anatomy_runner.py`
- Modify: `src/czsc_trader/dominant_exit_anatomy_runner.py`

**Interfaces:**
- Consumes: `generate_factor_frame`, `four_layer_runner._equivalence`, `score_four_layer`, `validate_experiment_archive`.
- Produces: `run_dominant_exit_anatomy(raw_dir, experiment_dir, protocol) -> dict[str, object]`.

- [ ] Add failing integration tests for source-hash drift, research-baseline drift, wrong event identities, a 2026 hash, factor mismatch, future trajectory rows, non-finite data, and forbidden candidate/order outputs.
- [ ] Regenerate the factor frame once, validate twelve identities and weights, extract all20 event trajectories, build signatures, classification, identity audit, metrics, and the causal SVG appendix.
- [ ] Verify every event has at most21 rows ending exactly on its signal date and every numeric artifact is finite.
- [ ] Run the focused test plus `tests/test_exit_signal_diagnosis_runner.py`, `tests/test_ex04_path_attribution_runner.py`, and `tests/test_experiment_archive.py`.
- [ ] Commit the tested runner implementation.

### Task 3: Stable experiment entrypoint

**Files:**
- Modify: `tests/test_experiment_entrypoints.py`
- Modify: `scripts/run_experiment.py`

**Interfaces:**
- Consumes: `run_dominant_exit_anatomy(...)`.
- Produces: `run_preregistered_dominant_exit_anatomy(experiment_dir: Path) -> Path` and CLI dispatch for `dominant_exit_path_anatomy`.

- [ ] Add a failing temporary-archive test proving in-place COMPLETE finalization, implementation SHA recording, evidence-specific conclusion, no holdout, no challenger, and CLI dispatch.
- [ ] Implement success and ERROR finalization, deterministic manifest construction, and forbidden-output checks.
- [ ] Run `.\.venv\Scripts\python.exe -m pytest -q tests/test_experiment_entrypoints.py tests/test_dominant_exit_anatomy_runner.py`.
- [ ] Commit the tested stable entrypoint.

### Task 4: Formal run, audit, and delivery

**Files:**
- Modify: `experiments/0825_EX05/03_execution.md`
- Modify: `experiments/0825_EX05/04_conclusion.md`
- Create: formal artifacts declared by the spec
- Modify: `experiments/0825_EX05/experiment_manifest.json`
- Modify: `docs/RESEARCH_HANDOFF.md`

- [ ] Commit the complete preregistration before any new target-event trajectory value is read.
- [ ] Execute `.\.venv\Scripts\python.exe scripts/run_experiment.py --experiment-dir experiments\0825_EX05` exactly once from the tested implementation SHA.
- [ ] Audit20 event identities, three fixed病灶, trajectory date bounds, twelve factors, descriptor/signature counts, protective contamination, finite values,18 visible 2020—2025 hashes, no2026, and no candidate/order files.
- [ ] Write the concrete conclusion without changing machine classification or promoting a trading rule.
- [ ] Update the handoff, rebuild and validate the manifest, run focused tests, compileall, all archive validation, structured EX05 audit, and `git diff --check`.
- [ ] Commit the final tracked archive. Do not merge or push without user instruction.
