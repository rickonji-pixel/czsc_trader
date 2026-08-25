# EX04 Exit Signal Diagnosis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Diagnose whether EX04's 20 divergent exit events are systematically poor, path-concentrated, context-dependent, mixed, or insufficient using exact event-level hold counterfactuals and only signal-date-known features.

**Architecture:** Add a focused runner that validates and consumes the tracked 0825_EX03 archive, loads only pre-2026 prices, and computes one isolated continue-holding counterfactual per frozen exit event. Keep event replay, labeling, Cliff's delta, context enrichment, concentration, and machine classification as pure tested functions; integrate the formal archive through the existing `scripts/run_experiment.py --experiment-dir` dispatch.

**Tech Stack:** Python 3.12, pandas, NumPy, pytest, existing market-data and experiment-archive primitives.

**Spec:** `experiments/0825_EX04/02_design.md`

## Global Constraints

- Work on `codex/0825-ex04-exit-signal-diagnosis`; no worktree or subagent.
- Formal inputs are exactly 20 tracked `early_ex04_exit` events from 0825_EX03.
- Visible cutoff is exactly `2025-12-31`; reject every loaded filename or event date containing 2026.
- Future prices may label outcomes but may not enter signal-date feature columns.
- Do not optimize, rank, freeze, call holdout, write orders, or alter either baseline.
- Formal status is `COMPLETE` or truthful `ERROR`, never strategy PASS/FAIL.
- Formal execution starts from a committed implementation SHA and runs once.

---

### Task 1: Pure event replay and diagnostic statistics

**Files:**
- Create: `src/czsc_trader/exit_signal_diagnosis_runner.py`
- Create: `tests/test_exit_signal_diagnosis_runner.py`

**Interfaces:**
- Produces: `replay_exit_counterfactual(prices, execution_date, endpoint_date, endpoint_reason, fee_rate) -> tuple[dict[str, object], pd.DataFrame]`
- Produces: `label_exit(log_advantage, materiality) -> str`
- Produces: `cliffs_delta(left, right) -> float`
- Produces: `summarize_contexts(events, axes, rules) -> pd.DataFrame`
- Produces: `classify_exit_quality(events, windows, contexts, features, protocol) -> dict[str, object]`

- [ ] Write a literal three-day replay test where an exit/re-entry path loses two fees relative to continuing to hold, plus baseline-exit and window-end endpoint tests.
- [ ] Run `.\.venv\Scripts\python.exe -m pytest -q tests/test_exit_signal_diagnosis_runner.py` and verify RED because the module is absent.
- [ ] Implement replay with normalized pre-exit wealth of1.0. For `ex04_reentry_execution`, actual terminal wealth is `(1-fee)/(1+fee)` while continued wealth is `endpoint_open/start_open`; for `baseline_exit_execution`, continued terminal wealth additionally multiplies by`1-fee`; for `window_end_close`, use final close without a terminal fee.
- [ ] Add literal boundary tests proving `+0.005` and `-0.005` are neutral because material labels require strict inequality.
- [ ] Add hand-derived Cliff's delta tests, including ties.
- [ ] Add context tests proving minimum support, minimum windows,75% target rate, net sign, and50% single-event cap are all required.
- [ ] Add classification tests for insufficient, concentrated, systematic, context-dependent, and mixed branches.
- [ ] Run the focused test and verify GREEN; refactor only after it remains green.

### Task 2: Formal pre-2026 exit diagnosis runner

**Files:**
- Modify: `src/czsc_trader/exit_signal_diagnosis_runner.py`
- Modify: `tests/test_exit_signal_diagnosis_runner.py`

**Interfaces:**
- Produces: `validate_protocol(protocol: Mapping[str, object]) -> None`
- Produces: `validate_source_archive(repository_root: Path, source: Mapping[str, object]) -> dict[str, object]`
- Produces: `run_exit_signal_diagnosis(raw_dir: Path, experiment_dir: Path, protocol: Mapping[str, object]) -> dict[str, object]`

- [ ] Write failing tests that reject protocol holdout drift, optimization flags, an event count other than20, duplicate IDs, source-manifest hash drift, a 2026 event date, a non-closing path ledger, and non-finite evidence.
- [ ] Validate the source with `validate_experiment_archive`, require `0825_EX03 / COMPLETE / holdout_accessed=false`, and compare every protocol-declared portable file hash with the source manifest.
- [ ] Load market data with cutoff`2025-12-31`, reject 2026 hashes, and read only the six frozen EX03 artifacts declared in the protocol.
- [ ] Filter exactly20 exit events, join the three group contribution rows per event, and derive score gaps, threshold margin, contribution gaps, trend-contribution sign, and consecutive prior EX04 holding days from the EX03 ledger.
- [ ] For each event, locate the first later EX04 re-entry execution or baseline exit execution inside its independent half-year; resolve same-day ties by protocol endpoint priority and otherwise use half-year last close.
- [ ] Replay each event, write the exact path ledger, verify per-event log-wealth closure within`1e-12`, and assign false/protective/neutral labels at strict`±0.005`.
- [ ] Aggregate by ten windows, underperformance/control cohort, four categorical axes, and14 continuous features; calculate concentration and apply the frozen classification order.
- [ ] Write every artifact named in the spec, plus identity and metrics JSON with `holdout_accessed=false` and `frozen_challenger=null`.
- [ ] Run `.\.venv\Scripts\python.exe -m pytest -q tests/test_exit_signal_diagnosis_runner.py tests/test_ex04_path_attribution_runner.py tests/test_experiment_archive.py tests/test_backtest.py`.

### Task 3: Stable experiment entrypoint

**Files:**
- Modify: `scripts/run_experiment.py`
- Modify: `tests/test_experiment_entrypoints.py`

**Interfaces:**
- Consumes: `run_exit_signal_diagnosis(...)`
- Produces: `run_preregistered_exit_signal_diagnosis(experiment_dir: Path) -> Path`

- [ ] Write a failing temporary-archive test proving same-directory finalization, COMPLETE manifest, no holdout, no challenger, evidence-specific conclusion text, and CLI dispatch for `ex04_exit_signal_diagnosis`.
- [ ] Implement protocol dispatch and validate returned status, hashes, event count, closure, forbidden files, holdout flag, and frozen challenger before writing docs.
- [ ] On success, write execution environment, implementation SHA, counts, closure, and machine classification; on exception, archive `ERROR` and re-raise without accessing 2026.
- [ ] Build and validate the experiment manifest in both paths.
- [ ] Run `.\.venv\Scripts\python.exe -m pytest -q tests/test_experiment_entrypoints.py tests/test_exit_signal_diagnosis_runner.py`.

### Task 4: Formal execution and delivery

**Files:**
- Modify: `experiments/0825_EX04/03_execution.md`
- Modify: `experiments/0825_EX04/04_conclusion.md`
- Create: formal artifacts declared by the spec
- Modify: `experiments/0825_EX04/experiment_manifest.json`
- Modify: `docs/RESEARCH_HANDOFF.md`

- [ ] Commit tested implementation, then execute `.\.venv\Scripts\python.exe scripts\run_experiment.py --experiment-dir experiments\0825_EX04` once from that SHA.
- [ ] Audit exactly20 unique events, all endpoint types, event-level closure, label counts, ten windows, loss/control cohorts, context support, continuous effects, concentration, finite numeric values, no 2026 hash, and no challenger/order files.
- [ ] Write the concrete conclusion without changing machine classifications; explicitly distinguish a diagnostic context from a deployable trading rule.
- [ ] Update the handoff, mark completed plan steps, and rebuild the manifest without rerunning the experiment.
- [ ] Run focused tests, compileall, all experiment-archive validation, the structured EX04 research audit, and `git diff --check`.
- [ ] Commit the final archive. Do not merge or push without user instruction.
