# EX04 Path Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Explain the three frozen EX04 underperformance windows by exact path, entry/exit event, score-margin, and causal hybrid-state-machine attribution using only 2020—2025 data.

**Architecture:** Add a focused `ex04_path_attribution_runner` that consumes the already-tested four-layer identities and backtester. Keep path labeling, dual-score state evolution, closure checks, and mechanism classification as pure tested functions; route the formal archive through the existing `scripts/run_experiment.py --experiment-dir` entrypoint.

**Tech Stack:** Python 3.12, pandas, NumPy, vectorbt, pytest, existing `czsc_trader` primitives.

**Spec:** `experiments/0825_EX03/02_design.md`

## Global Constraints

- Work on `codex/0825-ex03-path-attribution`; no worktree or subagent.
- Visible cutoff is exactly `2025-12-31`; reject every input hash containing `2026`.
- Do not optimize, rank, freeze, call holdout, or alter either baseline.
- Formal status is `COMPLETE` or truthful `ERROR`, never strategy PASS/FAIL.
- Formal execution starts from a committed implementation SHA.

---

### Task 1: Pure path and state-machine primitives

**Files:**
- Create: `src/czsc_trader/ex04_path_attribution_runner.py`
- Create: `tests/test_ex04_path_attribution_runner.py`

**Interfaces:**
- `positions_from_dual_scores(entry_scores, enter, exit_scores, exit, state_rule) -> pd.Series`
- `label_baseline_only_days(baseline_execution, ex04_execution) -> tuple[pd.DataFrame, pd.Series]`
- `classify_score_block(...) -> str`
- `classify_mechanism(mechanism_rows, hybrid_rows, protocol) -> dict[str, object]`

- [ ] Write literal tests for causal dual-score state evolution, fully missed/late/early/interrupted path labels, the five score-block labels, exact log-wealth closure, and every mechanism-classification branch.
- [ ] Run `.\.venv\Scripts\python.exe -m pytest -q tests/test_ex04_path_attribution_runner.py` and verify RED because the module is absent.
- [ ] Implement the minimal pure functions without I/O.
- [ ] Run the same test and verify GREEN.

### Task 2: Formal pre-2026 path runner

**Files:**
- Modify: `src/czsc_trader/ex04_path_attribution_runner.py`
- Modify: `tests/test_ex04_path_attribution_runner.py`

**Interface:**
- `run_ex04_path_attribution(raw_dir: Path, baseline_root: Path, experiment_dir: Path, protocol: Mapping[str, object]) -> dict[str, object]`

- [ ] Write failing validation tests for protocol drift, source-evidence hashes, EX04 identity, non-closing path ledgers, unclassified baseline-only days, and 2026 hashes.
- [ ] Implement baseline/EX04 identity checks, six frozen variants, independently funded half-year daily ledgers, regime-path aggregation, baseline episodes, score events, group contributions, hybrid metrics, classification, and required artifacts.
- [ ] Verify every window's log-wealth closure within `1e-12`, all baseline-only days classified, and all numeric outputs finite.
- [ ] Run `.\.venv\Scripts\python.exe -m pytest -q tests/test_ex04_path_attribution_runner.py tests/test_ex04_attribution_runner.py tests/test_four_layer_runner.py tests/test_backtest.py`.

### Task 3: Stable entrypoint integration

**Files:**
- Modify: `scripts/run_experiment.py`
- Modify: `tests/test_experiment_entrypoints.py`

**Interface:**
- `run_preregistered_ex04_path_attribution(experiment_dir: Path) -> Path`

- [ ] Write a failing temporary-archive test proving same-directory finalization, no holdout, no challenger, COMPLETE manifest, and protocol-based CLI dispatch.
- [ ] Implement EX03 dispatch, execution metadata, evidence-specific conclusion rendering, ERROR archival, manifest rebuild, and archive validation.
- [ ] Run `.\.venv\Scripts\python.exe -m pytest -q tests/test_experiment_entrypoints.py tests/test_ex04_path_attribution_runner.py`.

### Task 4: Formal execution and delivery

**Files:**
- Modify: `experiments/0825_EX03/03_execution.md`
- Modify: `experiments/0825_EX03/04_conclusion.md`
- Create: formal artifacts declared by the spec
- Modify: `experiments/0825_EX03/experiment_manifest.json`
- Modify: `docs/RESEARCH_HANDOFF.md`

- [ ] Commit tested implementation, then execute `.\.venv\Scripts\python.exe scripts\run_experiment.py --experiment-dir experiments\0825_EX03` once from that SHA.
- [ ] Audit 10-window closure, the three frozen loss windows, mechanism shares, score-event labels, six variants, finite numeric values, no 2026 hash, and no challenger.
- [ ] Write the concrete mechanism conclusion without changing machine classifications; update handoff and rebuild the manifest.
- [ ] Run focused tests, compileall, all experiment-archive validation, research audit, and `git diff --check`.
- [ ] Commit the final archive. Do not merge or push without user instruction.
