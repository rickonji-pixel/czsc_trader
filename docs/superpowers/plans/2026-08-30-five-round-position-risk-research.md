# Five-Round Position-Risk Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute five preregistered, causally audited 588080 position-risk experiments that seek unchanged fee-after return with strictly lower maximum drawdown.

**Architecture:** Add one focused feature/state module and one formal runner registered behind the existing `czsc-trader experiment run` interface. The first four rounds are bounded to 2021-2023 after 2020 warm-up; only the fifth runner may load locked 2024-2025 data and, after a second freeze gate, the observed 2026 historical segment.

**Tech Stack:** Python 3.12, pandas, NumPy, existing CZSC baseline application, vectorbt-backed period evaluator, independent ledger/audit framework, pytest.

**Spec:** `docs/superpowers/specs/2026-08-30-five-round-position-risk-research-design.md`

## Global Constraints

- Work on `codex/five-round-position-risk-research`; no worktrees or subagents.
- Resolve only active `baseline_20260826` from `configs/rule_baselines/registry.json`.
- Do not modify champion factors, weights, thresholds, state-machine dates, or registry identity.
- Overlay positions are exactly `0`, `0.50`, `0.75`, or `1.00`; no leverage or daily rebalancing.
- Signal T executes at the next trading-day open with one-way fee `0.0005` and initial cash `1,000,000`.
- Rounds 1-4 load with cutoff `2023-12-31`; round 5 alone may load `2025-12-31`, and may load `2026-08-28` only after a validation winner is frozen and hashed.
- Candidate spaces, eligibility, ranking, and stop rules are exactly those in the design spec.
- Technical ERROR retries preserve the same numerical protocol and do not consume an effective round.
- Locally commit preregistration, implementation, and completed archives as required for a clean formal chain; never push a remote.

---

### Task 1: Preregister the five-round program

**Files:**
- Create: `experiments/0830_EX03/{01_goal.md,02_design.md,implementation_plan.md,artifacts/protocol.json}`
- Create: `experiments/0830_EX04/{01_goal.md,02_design.md,implementation_plan.md,artifacts/protocol.json}`
- Create: `experiments/0830_EX05/{01_goal.md,02_design.md,implementation_plan.md,artifacts/protocol.json}`
- Create: `experiments/0830_EX06/{01_goal.md,02_design.md,implementation_plan.md,artifacts/protocol.json}`
- Create: `experiments/0830_EX07/{01_goal.md,02_design.md,implementation_plan.md,artifacts/protocol.json}`

**Interfaces:**
- Consumes: the committed design spec and immutable champion identity.
- Produces: five `position_risk_five_rounds` protocols with `program_round` values 1-5 and exact family grids.

- [ ] **Step 1: Write all human preregistration documents**

Record the objective, segment boundary, exact state mechanics, PASS/FAIL/COMPLETE status, no-access behavior, audit requirements, and observed-data disclosure. Round 1 is diagnostic; rounds 2-4 are discovery challenges; round 5 is locked validation.

- [ ] **Step 2: Write all exact machine protocols**

Each protocol contains this shared core:

```json
{
  "schema_version": 1,
  "handler": "position_risk_five_rounds",
  "status": "PRE_REGISTERED",
  "symbol": "588080.SH",
  "asset_type": "etf",
  "warmup_start": "2020-01-01",
  "discovery_start": "2021-01-01",
  "discovery_end": "2023-12-31",
  "validation_start": "2024-01-01",
  "validation_end": "2025-12-31",
  "historical_check_end": "2026-08-28",
  "fee_rate": 0.0005,
  "init_cash": 1000000.0
}
```

Add exact champion hashes, family parameters from the spec, eligibility fields, ranking fields, cutoffs, and predecessor archive identities. Round 5 references eligible frozen family winners by declared source experiment IDs rather than unknown future hashes.

- [ ] **Step 3: Validate JSON and the untouched repository diff**

Run: `Get-ChildItem experiments\0830_EX0[3-7]\artifacts\protocol.json | ForEach-Object { .\.venv\Scripts\python.exe -m json.tool $_.FullName > $null }`

Run: `git diff --check`

- [ ] **Step 4: Commit before implementing or evaluating market data**

```powershell
git add docs/superpowers experiments/0830_EX03 experiments/0830_EX04 experiments/0830_EX05 experiments/0830_EX06 experiments/0830_EX07
git commit -m "research: preregister five-round position-risk program"
```

### Task 2: Causal daily and intraday risk features

**Files:**
- Create: `src/czsc_trader/position_risk.py`
- Create: `tests/test_position_risk.py`

**Interfaces:**
- Produces: `RiskOverlaySpec(family: str, pressure_position: float, parameters: tuple[tuple[str, float], ...])` and deterministic `candidate_id`.
- Produces: `build_risk_features(daily: pd.DataFrame, intraday: pd.DataFrame) -> pd.DataFrame`.
- Feature columns: `close`, `ret_1d`, `sma_5`, `sma_20`, `prior_high_20`, `prior_high_60`, `prior_high_120`, `negative_count_5`, `negative_count_10`, `negative_count_20`, `close_location`, `down_volume_share`.

- [ ] **Step 1: Write failing daily-feature tests**

```python
def test_risk_features_exclude_current_bar_from_prior_high():
    features = build_risk_features(daily_fixture, intraday_fixture)
    assert features.loc[day_21, "prior_high_20"] == pytest.approx(
        daily_fixture.set_index("dt").loc[:day_20, "high"].tail(20).max()
    )
```

Also assert moving means and negative counts include only completed returns through T, indices are unique/increasing, and invalid OHLCV values raise `ValueError`.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_position_risk.py::test_risk_features_exclude_current_bar_from_prior_high -q`

Expected: FAIL because `czsc_trader.position_risk` does not exist.

- [ ] **Step 3: Write failing intraday aggregation tests**

```python
def test_intraday_pressure_uses_completed_same_day_bars():
    features = build_risk_features(daily_fixture, intraday_fixture)
    assert features.loc[signal_day, "close_location"] == pytest.approx(0.20)
    assert features.loc[signal_day, "down_volume_share"] == pytest.approx(0.75)
```

Assert a zero-range day gets close location `0.5`, zero total volume gets down-volume share `0.0`, and bars after T cannot affect T.

- [ ] **Step 4: Implement validated feature construction**

Normalize the `dt` column to a unique `DatetimeIndex`, aggregate intraday bars by normalized date, use `high.shift(1).rolling(L).max()` for prior highs, and compute down volume from bars where `close < open`. Join only matching completed dates and reject missing daily/intraday reconciliation.

- [ ] **Step 5: Run focused tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_position_risk.py -q`

Commit: `feat: add causal position-risk features`

### Task 3: Family state machines, targets, and events

**Files:**
- Modify: `src/czsc_trader/position_risk.py`
- Modify: `tests/test_position_risk.py`

**Interfaces:**
- Produces: `build_family_specs(family: str) -> tuple[RiskOverlaySpec, ...]` with counts trend=12, persistence=6, intraday=8.
- Produces: `build_pressure_state(features: pd.DataFrame, champion_target: pd.Series, spec: RiskOverlaySpec) -> pd.Series`.
- Produces: `compose_overlay_target(champion_target: pd.Series, pressure: pd.Series, pressure_position: float) -> pd.Series`.
- Produces: `build_position_risk_events(...) -> pd.DataFrame` with `Entry`, `Reduce`, `Increase`, and `Exit` provenance.

- [ ] **Step 1: Write failing exact-grid tests**

Assert exact candidate IDs, unique combinations, grid counts, `P in {0.5,0.75}`, and rejection of any unknown family or parameter.

- [ ] **Step 2: Write failing state-transition tests**

Use literal feature frames to prove each entry and recovery condition from the spec. Assert champion zero always clears pressure and produces target zero; pressure can never open a position.

- [ ] **Step 3: Implement minimal state machines**

Iterate dates in order, reset state when champion is zero, enter pressure only on the family trigger, persist until its exact recovery rule, and return a boolean Series. Compose positions without forward fills across champion exits.

- [ ] **Step 4: Write and pass event/audit tests**

Construct `0 -> 1 -> P -> 1 -> 0`; assert event types, parameters, causal feature values, before/after positions, and next-open order sides. Feed events to `run_period_backtests` and `audit_no_lookahead`.

- [ ] **Step 5: Run regression tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_position_risk.py tests\test_downside_risk.py tests\test_dynamic_position_sizing.py -q`

Commit: `feat: add interpretable position-risk state machines`

### Task 4: Formal protocol and evaluation runner

**Files:**
- Create: `src/czsc_trader/position_risk_runner.py`
- Modify: `src/czsc_trader/research/handlers.py`
- Modify: `tests/test_position_risk.py`

**Interfaces:**
- Produces: `validate_position_risk_protocol(protocol: Mapping[str, object], experiment_id: str) -> None`.
- Produces: `rank_eligible_position_risk(rows: pd.DataFrame) -> pd.DataFrame`.
- Produces: `run_position_risk_program(raw_dir: Path, baseline_root: Path, experiments_root: Path, experiment_dir: Path, *, execution_commit: str) -> dict[str, object]`.
- Registers handler `position_risk_five_rounds`.

- [ ] **Step 1: Write failing protocol-mutation tests**

Load all five protocols and assert success. Mutate one field at a time for champion hash, cutoffs, family grid, status, fee, ranking, eligibility, predecessor identity, and `program_round`; assert `ValueError`.

- [ ] **Step 2: Write failing eligibility and stable-ranking tests**

Literal rows prove that lower return or equal/worse drawdown is excluded. Eligible rows sort by drawdown improvement descending, worst annual return delta descending, full return delta descending, transition count ascending, candidate ID ascending.

- [ ] **Step 3: Write cutoff-isolation tests**

Monkeypatch `load_market_data` and assert rounds 1-4 call it exactly once with cutoff `2023-12-31`. Round 5 may first call `2025-12-31`; it must not call `2026-08-28` when validation has no eligible candidate.

- [ ] **Step 4: Implement diagnostic and family evaluation paths**

Round 1 writes state-level forward-return, adverse-excursion, annual-sign, overlap, and concentration artifacts and completes without a challenger. Rounds 2-4 evaluate discovery FULL plus 2021, 2022, and 2023, write every candidate row, freeze only an eligible family winner, and produce truthful PASS/FAIL documents.

- [ ] **Step 5: Implement locked round-5 path**

Validate source archives and hashes, load only family PASS winners, evaluate 2024-2025 FULL plus 2024 and 2025, freeze at most one eligible winner, then and only then load 2026 and evaluate 2026FULL plus diagnostics. Do not create a 2026 artifact if an earlier gate fails.

- [ ] **Step 6: Implement ERROR archives and handler registration**

Any identity, protocol, causality, ledger, file, or runtime exception writes `error.json`, true segment-access flags, ERROR documents, and a validated manifest before re-raising.

- [ ] **Step 7: Run tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_position_risk.py tests\test_cli_e2e.py -q`

Commit: `feat: run five-round position-risk research`

### Task 5: Execute and archive round 1

**Files:**
- Generate: `experiments/0830_EX03/03_execution.md`
- Generate: `experiments/0830_EX03/04_conclusion.md`
- Generate: `experiments/0830_EX03/experiment_manifest.json`
- Generate: `experiments/0830_EX03/artifacts/*`

- [ ] **Step 1: Verify clean committed implementation**

Run: `git status --short --branch`

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_position_risk.py -q`

- [ ] **Step 2: Execute exactly once**

Run: `.\.venv\Scripts\czsc-trader.exe experiment run --dir experiments\0830_EX03 --repo-root .`

- [ ] **Step 3: Read every generated file and validate archive**

Run: `.\.venv\Scripts\czsc-trader.exe archive validate --archive experiments\0830_EX03`

Confirm `COMPLETE`, cutoff 2023-12-31, no challenger, and no later-segment access.

- [ ] **Step 4: Commit the diagnostic archive locally**

Commit: `research: archive holding-hazard attribution`

### Task 6: Execute and archive rounds 2-4

**Files:**
- Generate: `experiments/0830_EX04/{03_execution.md,04_conclusion.md,experiment_manifest.json,artifacts/*}`
- Generate: `experiments/0830_EX05/{03_execution.md,04_conclusion.md,experiment_manifest.json,artifacts/*}`
- Generate: `experiments/0830_EX06/{03_execution.md,04_conclusion.md,experiment_manifest.json,artifacts/*}`

- [ ] **Step 1: Run round 2 from a clean committed state**

Run: `.\.venv\Scripts\czsc-trader.exe experiment run --dir experiments\0830_EX04 --repo-root .`

Read the entire archive, validate it, and locally commit PASS or FAIL without changing its grid.

- [ ] **Step 2: Run round 3 from a clean committed state**

Run: `.\.venv\Scripts\czsc-trader.exe experiment run --dir experiments\0830_EX05 --repo-root .`

Read the entire archive, validate it, and locally commit PASS or FAIL without using round-2 performance to modify the protocol.

- [ ] **Step 3: Run round 4 from a clean committed state**

Run: `.\.venv\Scripts\czsc-trader.exe experiment run --dir experiments\0830_EX06 --repo-root .`

Read the entire archive, validate it, and locally commit PASS or FAIL without using earlier performance to modify the protocol.

- [ ] **Step 4: Audit the discovery boundary**

Assert all three manifests report visible end `2023-12-31`, no validation/2026 access, and any frozen winner hash matches its bytes.

### Task 7: Execute locked round 5

**Files:**
- Generate: `experiments/0830_EX07/{03_execution.md,04_conclusion.md,experiment_manifest.json,artifacts/*}`

- [ ] **Step 1: Verify all source archives and clean status**

Run archive validation for EX03-EX06 and verify local commits contain every source result.

- [ ] **Step 2: Execute the locked tournament exactly once**

Run: `.\.venv\Scripts\czsc-trader.exe experiment run --dir experiments\0830_EX07 --repo-root .`

- [ ] **Step 3: Read and verify the entire archive**

Confirm source identities, candidate count, validation metrics, freeze hash if any, exact access flags, 2026 metrics if permitted, events, orders, and ledger audit.

- [ ] **Step 4: Commit the final formal archive locally**

Commit: `research: archive five-round position-risk result`

### Task 8: Update handoff and complete local verification

**Files:**
- Modify: `tests/test_cli_e2e.py`
- Modify: `docs/RESEARCH_HANDOFF.md`

- [ ] **Step 1: Update tracked archive expectations**

Increment the exact archive count and append every actual experiment directory, including truthful technical ERROR retries.

- [ ] **Step 2: Update the cross-device handoff**

Record all five effective statuses, exact segment access, family winners, locked validation and 2026 facts, active-baseline non-change, branch name, and no-push state.

- [ ] **Step 3: Run complete final verification**

Run pip check, data validation, baseline validation, archive validation, full pytest, compileall, and `git diff --check`. Every command must exit zero.

- [ ] **Step 4: Commit locally and verify remote remains untouched**

Commit: `docs: hand off five-round position-risk research`

Verify local branch has no upstream and `origin/master` remains at its pre-program SHA. Do not run `git push`.
