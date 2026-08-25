# 0825_EX06 New Exit Representation Diagnosis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Determine whether 28 preregistered causal structure, energy, volume, intraday, and weekly descriptors distinguish the three dominant false exits from the eleven protective exits of the `0824_EX04` research baseline.

**Architecture:** A focused runner loads only tracked 2020—2025 market data and the frozen `0825_EX04` event cohort, computes the 28 descriptors for every eligible historical date without using event outcomes, assigns causal trailing-history quantile bins, and applies sequential discovery, confirmation, contamination, and exact-permutation classification. The existing stable experiment entrypoint finalizes the same preregistered archive and explicitly forbids orders, candidates, frozen challengers, and holdout access.

**Tech Stack:** Python 3.12, pandas, NumPy, pytest, tracked experiment archives.

**Spec:** `experiments/0825_EX06/01_goal.md` and `experiments/0825_EX06/02_design.md`

## Global Constraints

- Research baseline is `0824_EX04`; event identities and labels come from `0825_EX04`; `0825_EX05` is prior negative evidence only.
- Compute exactly 28 descriptors with the formulas and causal percentile histories frozen in the spec.
- Visible data ends at `2025-12-31`; no 2026 file, timestamp, holdout, optimizer, classifier, feature interaction, order, candidate, or frozen challenger.
- The formal experiment runs exactly once from a clean implementation commit; all outcomes are archived without changing thresholds or descriptors.
- Work in the current branch without worktrees or subagents.

---

### Task 1: Pure descriptor, binning, and permutation core

**Files:**
- Create: `tests/test_new_exit_representation_runner.py`
- Create: `src/czsc_trader/new_exit_representation_runner.py`

**Interfaces:**
- Produces: `validate_protocol(protocol: Mapping[str, object]) -> None`
- Produces: `compute_daily_descriptors(daily: pd.DataFrame) -> pd.DataFrame`
- Produces: `compute_intraday_descriptors(intraday: pd.DataFrame) -> pd.DataFrame`
- Produces: `compute_weekly_descriptor(daily_dates: pd.Series, weekly: pd.DataFrame) -> pd.Series`
- Produces: `assign_causal_quantile_bins(frame: pd.DataFrame, protocol: Mapping[str, object]) -> pd.DataFrame`
- Produces: `discover_and_confirm_signatures(events: pd.DataFrame, matrix: pd.DataFrame, protocol: Mapping[str, object]) -> tuple[pd.DataFrame, pd.DataFrame]`
- Produces: `exact_permutation_audit(events: pd.DataFrame, matrix: pd.DataFrame, protocol: Mapping[str, object]) -> dict[str, object]`
- Produces: `classify_representation(...) -> dict[str, object]`

- [ ] **Step 1: Write failing formula and causality tests**

Create deterministic OHLCV fixtures and assert literal values for representative formulas from all four families, exact column order of 28 descriptors, completed-week alignment, division-by-zero to `NaN`, exclusion of the current observation from historical quantiles, right-closed lower-bin boundary behavior, and no timestamps after event T.

```python
def test_descriptor_frame_has_frozen_identity_and_finite_or_nan_values():
    frame = compute_daily_descriptors(_daily_fixture())
    assert frame.columns.tolist() == ["dt", *DESCRIPTOR_IDS[:23]]
    assert frame.loc[20, "close_to_ma20"] == pytest.approx(
        frame.loc[20, "close"] / frame.loc[1:20, "close"].mean() - 1
    )

def test_causal_bins_exclude_current_value():
    raw = pd.DataFrame({"dt": pd.date_range("2020-01-01", periods=6), "x": [1, 2, 3, 4, 5, 100]})
    bins = assign_causal_quantile_bins(raw, _protocol(daily_history_window=5, daily_minimum_history=5))
    assert bins.loc[5, "x"] == "Q5"
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_new_exit_representation_runner.py`

Expected: collection fails because `czsc_trader.new_exit_representation_runner` does not exist.

- [ ] **Step 3: Implement the 28 formulas and causal binning**

Use named constants to freeze identity and order, vectorized rolling operations for daily history, one daily aggregation for 30-minute inputs, and `merge_asof(..., direction="backward", allow_exact_matches=True)` against completed weekly rows.

```python
DESCRIPTOR_IDS = (
    "close_to_ma20", "ma20_slope5", "ma10_ma20_spread",
    "drawdown_from_high20", "close_location20", "return5", "return10",
    "prior_low10_buffer", "atr5_to_atr20", "tr_to_atr20",
    "realized_vol5_to20", "downside_semivol5_to20", "negative_day_share5",
    "max_drawdown5_to_atr20", "daily_close_location", "gap_abs_to_atr20",
    "volume5_to20", "volume_t_to20", "down_up_volume_ratio10",
    "signed_volume_imbalance5", "signed_volume_imbalance10",
    "return_volume_corr10", "log_volume_slope5", "intraday_down_volume_share",
    "intraday_realized_vol_to20", "intraday_close_location",
    "last4_30m_return", "weekly_close_to_ma10",
)
```

- [ ] **Step 4: Add and run signature/permutation classification tests**

Cover all five machine classes, exact enumeration count `364`, a qualifying observed label set with corrected `p<=0.05`, a low-contamination signature rejected by multiplicity, downtrend and `joint_margin_block` vetoes, neutral/secondary false exits excluded from the primary denominator, and missing-history evidence failure.

```python
assert audit["combination_count"] == 364
assert audit["exact_p_value"] == pytest.approx(
    audit["qualifying_combination_count"] / 364
)
assert classification["classification"] in {
    "insufficient_new_representation_evidence",
    "new_representation_confirmed",
    "suggestive_but_multiplicity_unconfirmed",
    "shared_but_contaminated_new_representation",
    "no_shared_new_representation",
}
```

- [ ] **Step 5: Run focused tests and commit the pure core**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_new_exit_representation_runner.py`

Expected: PASS.

Commit: `research: implement EX06 representation core`

### Task 2: Formal causal runner and fixed artifacts

**Files:**
- Modify: `tests/test_new_exit_representation_runner.py`
- Modify: `src/czsc_trader/new_exit_representation_runner.py`

**Interfaces:**
- Consumes: `load_market_data`, `validate_experiment_archive`, and source hashes from the frozen protocol.
- Produces: `run_new_exit_representation(raw_dir: Path, experiment_dir: Path, protocol: Mapping[str, object]) -> dict[str, object]`
- Produces exactly the nine artifacts listed in `02_design.md`.

- [ ] **Step 1: Write failing integration and identity tests**

Test source archive drift, baseline drift, prior-negative-result drift, novelty-list drift, wrong event identities, descriptor identity drift, fewer than 20 events, 2026 hashes, future timestamps, non-finite exported values, and forbidden output names.

```python
with pytest.raises(ValueError, match="2026"):
    validate_visible_hashes({"588080_daily_2026.csv": "abc"})

assert set(summary) >= {
    "status", "visible_data_hashes", "event_count", "descriptor_count",
    "confirmed_signature_count", "exact_p_value", "representation_classification",
}
assert summary["holdout_accessed"] is False
assert summary["frozen_challenger"] is None
```

- [ ] **Step 2: Implement one-pass formal runner**

Load the market bundle once with cutoff `2025-12-31`; calculate all historical descriptor rows before joining the fixed events; export raw values and bins in long form; write descriptor definitions, discovery/confirmation tables, permutation audit, identity audit, classification, and metrics with deterministic column order and UTF-8 JSON.

```python
def run_new_exit_representation(raw_dir, experiment_dir, protocol):
    validate_protocol(protocol)
    # Validate tracked sources before loading market values.
    # Compute descriptors without outcome columns, then join fixed event labels.
    # Apply the frozen machine pipeline and write only declared artifacts.
    return metrics
```

- [ ] **Step 3: Run focused and neighboring regression tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_new_exit_representation_runner.py tests/test_dominant_exit_anatomy_runner.py tests/test_exit_signal_diagnosis_runner.py tests/test_experiment_archive.py`

Expected: PASS.

- [ ] **Step 4: Commit the tested formal runner**

Commit: `research: add EX06 representation runner`

### Task 3: Stable `run_experiment.py` entrypoint

**Files:**
- Modify: `tests/test_experiment_entrypoints.py`
- Modify: `scripts/run_experiment.py`

**Interfaces:**
- Consumes: `run_new_exit_representation(...)`.
- Produces: `run_preregistered_new_exit_representation(experiment_dir: Path) -> Path`.
- Adds CLI dispatch for `experiment_type == "new_exit_representation_diagnosis"`.

- [ ] **Step 1: Write failing in-place finalization and CLI tests**

Use a temporary preregistered archive and fake runner to prove the entrypoint records the execution SHA, renders the exact machine class and counts, builds a COMPLETE manifest in the same directory, rejects promotion flags, creates no order/candidate/frozen file, and is selected by CLI dispatch.

```python
finalized = entrypoint.run_preregistered_new_exit_representation(archive)
assert finalized == archive
assert validate_experiment_archive(archive)["status"] == "COMPLETE"
assert "0824_EX04" in (archive / "04_conclusion.md").read_text(encoding="utf-8")
```

- [ ] **Step 2: Run entrypoint tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_experiment_entrypoints.py -k new_exit_representation`

Expected: FAIL because the entrypoint and CLI dispatch are absent.

- [ ] **Step 3: Implement success and ERROR finalization**

Follow the existing `0825_EX05` in-place diagnostic pattern. On success, write concrete execution and conclusion documents from machine artifacts; on failure, archive `ERROR`, retain `holdout_accessed=false`, validate the manifest, and re-raise.

- [ ] **Step 4: Run entrypoint plus runner tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_experiment_entrypoints.py tests/test_new_exit_representation_runner.py`

Expected: PASS.

Commit: `research: add stable EX06 diagnosis entrypoint`

### Task 4: Clean execution commit and formal run

**Files:**
- Modify: `experiments/0825_EX06/implementation_plan.md`
- No result artifacts before the clean implementation commit.

**Interfaces:**
- Produces an immutable execution SHA for the formal archive.

- [ ] **Step 1: Run implementation verification**

Run focused tests from Tasks 1—3, `\.\.venv\Scripts\python.exe -m compileall -q src tests scripts`, `git diff --check`, and validate all tracked experiment archives.

Expected: every command exits zero.

- [ ] **Step 2: Mark Tasks 1—3 complete and commit the clean execution version**

Commit: `research: freeze EX06 diagnosis implementation`

- [ ] **Step 3: Verify clean tree and record HEAD**

Run: `git status --short --branch` and `git rev-parse HEAD`.

Expected: no modified/untracked files; the returned SHA becomes the formal execution commit.

- [ ] **Step 4: Execute exactly once**

Run: `.\.venv\Scripts\python.exe scripts\run_experiment.py --experiment-dir experiments\0825_EX06`

Expected: the command returns the actual finalized `0825_EX06` directory and does not invoke `run_holdout.py`.

### Task 5: Audit, handoff, and final archive

**Files:**
- Modify: `experiments/0825_EX06/03_execution.md`
- Modify: `experiments/0825_EX06/04_conclusion.md`
- Create: the nine fixed formal artifacts from the spec
- Modify: `experiments/0825_EX06/experiment_manifest.json`
- Modify: `experiments/0825_EX06/implementation_plan.md`
- Modify: `docs/RESEARCH_HANDOFF.md`

**Interfaces:**
- Produces a portable COMPLETE or ERROR research archive and the next-session handoff.

- [ ] **Step 1: Audit the formal result without changing its machine class**

Verify 20 unique event identities, 28 descriptor identities and formulas, causal history bounds, three fixed dominant dates, 11 protective events, 364 permutations, exact p-value arithmetic, finite exported numeric values, tracked 2020—2025 hashes only, and absence of orders/candidates/frozen challengers.

- [ ] **Step 2: Update the handoff from machine artifacts**

State the concrete classification, qualifying or rejected signatures, contamination, exact p-value, no-2026 audit, and whether a subsequent independent strategy experiment is allowed. Do not describe a diagnostic signature as a trading rule.

- [ ] **Step 3: Rebuild and validate the final archive**

Run archive validation for `0825_EX06`, then all tracked experiment directories, compileall, focused tests, and `git diff --check`.

Expected: every command exits zero.

- [ ] **Step 4: Commit the final tracked archive**

Commit: `research: archive 0825 EX06 representation diagnosis`

- [ ] **Step 5: Run post-commit verification**

Run focused tests, all archive validation, `git status --short --branch`, and `git log -5 --oneline --decorate`.

Expected: tests and archives pass, branch is clean, and no merge or push occurs without a new user instruction.
