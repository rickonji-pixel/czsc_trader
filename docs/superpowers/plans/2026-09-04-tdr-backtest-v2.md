# TDR Backtest v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the legacy baseline-oriented TDR backtest with a deterministic strategy-snapshot replay engine that uses explicit dates, correct unadjusted execution prices, SE-owned audits, and independent research/backtest datasets.

**Architecture:** Add a focused `czsc_trader.backtesting` package whose pure core accepts an immutable strategy snapshot and validated market-data bundle. Formal-strategy and research-candidate adapters resolve different identities into that common input. Keep the legacy runner only long enough to explain migration differences, then remove its production entry and all runtime `configs` dependencies.

**Tech Stack:** Python 3.12, pandas, NumPy, Plotly, existing CZSC factor pipeline, `strategy_manager`, `strategy_evaluator`, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-04-tdr-backtest-v2-design.md`

## Global Constraints

- Do not use a git worktree; execute on `codex/tdr-backtest-v2`.
- One run handles one symbol, one strategy snapshot, one explicit date interval, and one independently funded account.
- `start`, `end`, and `initial_cash` are required; the account starts with cash and zero holdings.
- The replay core is lifecycle-state agnostic; only PTE deployment enforces `PAPER_READY`.
- Signal generation uses adjusted market data; pricing, fills, fees, quantity, and account valuation use unadjusted execution data.
- Normal runs use the execution policy embedded in the strategy snapshot; only SE stress scenarios may override fee or slippage assumptions.
- Backtest records dataset facts and never judges data pollution.
- Research dataset cutoff governance is outside this implementation.
- `configs` must have no production-code consumers when the migration completes.
- Preserve historical experiment artifacts; archive referenced legacy inputs before deleting their old locations.
- Keep tests at functional-contract granularity in line with OPC test governance.

---

### Task 1: Introduce the strategy-snapshot contract

**Files:**
- Create: `src/czsc_trader/backtesting/__init__.py`
- Create: `src/czsc_trader/backtesting/models.py`
- Create: `src/czsc_trader/backtesting/strategy_source.py`
- Modify: `src/czsc_trader/application/context.py`
- Modify: `tests/functional/test_strategy_lifecycle.py`

**Interfaces:**
- Produces: `StrategyIdentity`, `StrategySnapshot`, `resolve_registered_strategy(context, strategy_id, version)`, and `resolve_candidate_snapshot(candidate_id, strategy_payload, content_hash, source)`.
- Consumes: `strategy_manager.StrategyRegistry`, `czsc_trader.baselines.resolve_strategy_payload` during the migration only.

- [ ] **Step 1: Write failing functional tests for both strategy sources**

Add tests that assert a registered `S001-v1` and an in-memory candidate resolve to the same frozen rule shape while preserving distinct identities:

```python
registered = resolve_registered_strategy(context, "S001", "v1")
candidate = resolve_candidate_snapshot(
    "0904_EX04:R1102", payload, candidate_hash, "experiments/0904_EX04"
)
assert registered.identity.kind == "REGISTERED"
assert registered.identity.reference == "S001-v1"
assert candidate.identity.kind == "CANDIDATE"
assert candidate.identity.reference == "0904_EX04:R1102"
assert registered.content_hash
assert candidate.content_hash == candidate_hash
```

- [ ] **Step 2: Run the targeted test and confirm the missing-module failure**

Run: `pytest tests/functional/test_strategy_lifecycle.py -q`

Expected: FAIL because `czsc_trader.backtesting.strategy_source` does not exist.

- [ ] **Step 3: Implement immutable snapshot models and adapters**

Use explicit dataclasses:

```python
@dataclass(frozen=True)
class StrategyIdentity:
    kind: Literal["REGISTERED", "CANDIDATE"]
    reference: str
    source: str

@dataclass(frozen=True)
class StrategySnapshot:
    identity: StrategyIdentity
    content_hash: str
    strategy_payload: Mapping[str, object]
    resolved_rule: ResolvedBaseline
```

Registered resolution validates the SM release hash but does not call `assert_deployable`. Candidate resolution requires a caller-provided content hash and complete strategy payload.

- [ ] **Step 4: Move the default SM storage path in repository context**

Change `RepositoryContext.strategy_root` from `root / "configs" / "strategies"` to `root / "strategies"`. Do not move files until Task 8; tests may explicitly create the new root.

- [ ] **Step 5: Run tests and commit**

Run: `pytest tests/functional/test_strategy_lifecycle.py -q`

Expected: PASS.

Commit: `feat: add backtest strategy snapshot contract`

---

### Task 2: Add explicit dataset resolution and independent backtest data

**Files:**
- Create: `src/czsc_trader/backtesting/datasets.py`
- Modify: `src/czsc_trader/data.py`
- Modify: `src/czsc_trader/application/context.py`
- Modify: `src/czsc_trader/application/data_service.py`
- Modify: `src/czsc_trader/cli/main.py`
- Modify: `tests/functional/test_data_and_advice.py`
- Create: `data/backtest/` from the validated `data/raw` seed

**Interfaces:**
- Produces: `DatasetName = Literal["research", "backtest"]`, `ReplayData`, `load_replay_data(context, dataset, symbol, asset_type, cutoff)`, and `update_backtest_data(...)`.
- `ReplayData` contains adjusted `MarketData`, unadjusted daily and 30-minute execution frames, a manifest, and a cutoff-specific fingerprint.

- [ ] **Step 1: Write failing dataset-contract tests**

Cover explicit selection, cutoff truncation, adjusted/unadjusted date alignment, stable fingerprinting, and rejection of an attempt to alter an already published date:

```python
data = load_replay_data(context, "backtest", "588080.SH", "etf", date(2026, 9, 2))
assert data.adjusted.daily["dt"].max().date() == date(2026, 9, 2)
assert data.execution_daily["dt"].max().date() == date(2026, 9, 2)
assert data.fingerprint == load_replay_data(...).fingerprint
```

- [ ] **Step 2: Run tests and confirm missing dataset API**

Run: `pytest tests/functional/test_data_and_advice.py -q`

Expected: FAIL on missing `load_replay_data`.

- [ ] **Step 3: Implement dataset roots and cutoff fingerprints**

Extend `RepositoryContext` with `research_data_root` and `backtest_data_root`. Hash only rows visible through `cutoff`, together with normalized symbol, asset type, frequencies, adjustment semantics, and schema version.

- [ ] **Step 4: Produce unadjusted 30-minute execution data**

Update the market-data preparation path to publish unadjusted 30-minute bars alongside unadjusted daily bars. Validate that adjusted and execution calendars cover the same requested sessions. Backtest v2 must not reuse adjusted intraday lows for execution.

- [ ] **Step 5: Implement explicit, atomic backtest-data updates**

Add `czsc-trader data update-backtest --symbol <symbol> --asset <asset> --through <date>`. It writes to staging, rejects mutations to existing rows, validates manifests and hashes, and then atomically replaces the published dataset. A failed network or validation call leaves the previous dataset untouched. `backtest run` never invokes this updater implicitly.

- [ ] **Step 6: Seed and validate `data/backtest`**

Initialize the directory from current research data, generating the new execution 30-minute files and manifest. Run both dataset validators and confirm identical coverage through 2026-09-02.

- [ ] **Step 7: Run tests and commit**

Run: `pytest tests/functional/test_data_and_advice.py -q`

Expected: PASS.

Commit: `feat: add independent replay datasets`

---

### Task 3: Extract the shared order-intent policy

**Files:**
- Create: `src/czsc_trader/execution_intent.py`
- Modify: `src/czsc_trader/application/advice_service.py`
- Modify: `src/czsc_trader/execution_policy.py`
- Modify: `tests/functional/test_data_and_advice.py`

**Interfaces:**
- Produces: `calculate_entry_limit(execution_close, execution_spec) -> float`, `calculate_target_quantity(available_cash, limit_price, fee_rate, lot_size) -> int`, and `decide_order_intent(target_position, actual_quantity, cycle_target_quantity, available_cash, execution_close, execution_spec) -> OrderIntent`.
- Consumed by: advice and Backtest v2 execution replay.

- [ ] **Step 1: Write failing advice/backtest parity tests**

Use the 2026-01-05 prices to lock the corrected semantics:

```python
intent = decide_order_intent(
    target_position=1,
    actual_quantity=0,
    cycle_target_quantity=None,
    available_cash=1_000_000,
    execution_close=1.430,
    execution_spec=spec,
)
assert intent.limit_price == 1.430
assert intent.quantity == 698_900
```

Assert advice emits the same limit price and quantity for the same inputs.

- [ ] **Step 2: Run tests and verify failure against adjusted-price behavior**

Run: `pytest tests/functional/test_data_and_advice.py -q`

Expected: FAIL because the shared intent API is absent.

- [ ] **Step 3: Implement pure intent functions**

Move the latest advice quantity and pricing semantics into `execution_intent.py`. Retain previous advice schema builders only where existing PTE compatibility still needs them, with v4 delegating all order calculations to the new functions.

- [ ] **Step 4: Make execution simulation consume the shared policy**

Remove independent price/quantity calculations from the new replay path. Keep legacy behavior isolated until Task 10.

- [ ] **Step 5: Run tests and commit**

Run: `pytest tests/functional/test_data_and_advice.py -q`

Expected: PASS.

Commit: `refactor: share TDR order intent policy`

---

### Task 4: Build deterministic signal and account replay

**Files:**
- Create: `src/czsc_trader/backtesting/signal_replay.py`
- Create: `src/czsc_trader/backtesting/execution_replay.py`
- Create: `src/czsc_trader/backtesting/result.py`
- Modify: `tests/functional/test_backtest.py`

**Interfaces:**
- Produces: `replay_signals(snapshot, replay_data, start, end) -> SignalReplay` and `replay_account(signal_replay, replay_data, initial_cash, scenario) -> BacktestResult`.
- `BacktestResult` exposes normalized `decisions`, `orders`, `fills`, `account_daily`, `trades`, and metric-ready equity.

- [ ] **Step 1: Replace the legacy happy-path test with a v2 contract test**

The test resolves `S001-v1`, runs 2026-01-01 through 2026-09-02 with 100,000 cash, and asserts:

```python
assert result.identity.reference == "S001-v1"
assert result.account_daily.iloc[0]["cash_before"] == 100_000
assert result.orders["quantity"].mod(100).eq(0).all()
assert set(result.fills["trigger"]) <= {"OPEN", "INTRADAY_LIMIT"}
assert result.decisions["signal_date"].max() <= pd.Timestamp("2026-09-02")
```

- [ ] **Step 2: Add focused state-transition cases to the same functional test**

Cover first-session initial entry, open fill, strict intraday fill, equal-touch unfilled, repeated entry attempts with a stable cycle quantity, exit at next open, and an unclosed tail position.

- [ ] **Step 3: Run the test and confirm missing replay functions**

Run: `pytest tests/functional/test_backtest.py -q`

Expected: FAIL on imports from `czsc_trader.backtesting`.

- [ ] **Step 4: Implement signal replay**

Load sufficient pre-start calculation context, apply the resolved rule through `end`, create deterministic decision IDs, and slice published decisions to the evaluation account timeline. Record calculation-context and evaluation ranges separately.

- [ ] **Step 5: Implement the account state machine**

Start with cash and zero holdings. Process each valid session in this order: prior-session decision, order intent, virtual fill, cash/holding mutation, then close valuation. Store unfilled attempts as orders; store only confirmed simulated executions as fills. Keep cycle target quantity stable until fill, cancellation by target change, or cycle end.

- [ ] **Step 6: Build complete trade pairs and equity**

Pair fills by cycle ID. Include an open-tail row with explicit `OPEN` status but exclude it from win/loss calculations.

- [ ] **Step 7: Run tests and commit**

Run: `pytest tests/functional/test_backtest.py -q`

Expected: PASS.

Commit: `feat: add deterministic backtest replay core`

---

### Task 5: Implement metrics and immutable output publication

**Files:**
- Create: `src/czsc_trader/backtesting/metrics.py`
- Create: `src/czsc_trader/backtesting/evidence.py`
- Create: `src/czsc_trader/backtesting/report.py`
- Create: `src/czsc_trader/backtesting/service.py`
- Modify: `src/czsc_trader/charting.py`
- Modify: `tests/functional/test_backtest.py`

**Interfaces:**
- Produces: `calculate_metrics(result)`, `build_manifest(request, snapshot, data, result, audit)`, and `run_backtest_v2(...) -> BacktestRunSummary`.
- Publishes: `manifest.json`, `decisions.csv`, `orders.csv`, `fills.csv`, `account_daily.csv`, `trades.csv`, `metrics.json`, `audit.json`, `report.md`, and `chart.html`.

- [ ] **Step 1: Extend the functional test with the complete artifact contract**

Assert exact required filenames, non-overwriting output paths, strategy and dataset hashes in the manifest, and separate calculation/evaluation ranges.

- [ ] **Step 2: Add metric semantics assertions**

Require `max_drawdown`, `calmar`, `win_loss_ratio`, `win_loss_ratio_status`, `return`, `sharpe`, and `closed_trades`. Verify `NO_CLOSED_TRADES`, `NO_WINS`, `NO_LOSSES`, and `VALID` use only closed cycles.

- [ ] **Step 3: Run tests and confirm missing artifacts**

Run: `pytest tests/functional/test_backtest.py -q`

Expected: FAIL because v2 publication is not implemented.

- [ ] **Step 4: Implement metrics and evidence serialization**

Reuse mathematically valid functions from `strategy_metrics.py`, moving or delegating them into the new package. Calculate all outputs from the unadjusted account ledger.

- [ ] **Step 5: Implement report and chart**

Use the strategy/candidate reference as the title. Label fills as virtual fills. Show the core OPC metrics first, with return and Sharpe as supporting metrics.

- [ ] **Step 6: Implement staged publication**

Write every file into a temporary run directory, fsync/close writers, run structural validation, and atomically publish a unique final directory. On failure, return a structured command error and preserve no apparently complete run.

- [ ] **Step 7: Run tests and commit**

Run: `pytest tests/functional/test_backtest.py -q`

Expected: PASS.

Commit: `feat: publish backtest v2 evidence`

---

### Task 6: Add SE-owned replay evidence auditing

**Files:**
- Modify: `packages/strategy_evaluator/src/strategy_evaluator/audit_models.py`
- Modify: `packages/strategy_evaluator/src/strategy_evaluator/engineering_audit.py`
- Modify: `packages/strategy_evaluator/src/strategy_evaluator/__init__.py`
- Modify: `packages/strategy_evaluator/tests/functional/test_strategy_evaluator.py`
- Create: `src/czsc_trader/backtesting/audit_adapter.py`
- Modify: `tests/functional/test_backtest.py`

**Interfaces:**
- Produces in SE: `ReplayEvidence`, `ReplayAuditResult`, `audit_replay(evidence) -> ReplayAuditResult`, and `hash_replay_evidence(evidence) -> str`.
- Produces in TDR: `build_replay_evidence(snapshot, data, result) -> ReplayEvidence`.

- [ ] **Step 1: Add SE functional tests for valid and tampered evidence**

Create one complete replay fixture and verify PASS. Mutate each of strategy hash, data hash, decision date, order limit, lot size, fill trigger, cash, holdings, fee, equity, trade pairing, and repeated-run content; assert stable failure reason codes.

- [ ] **Step 2: Run the SE test and confirm missing types**

Run: `pytest packages/strategy_evaluator/tests/functional/test_strategy_evaluator.py -q`

Expected: FAIL on missing `ReplayEvidence`.

- [ ] **Step 3: Implement exact replay evidence models**

Use immutable records containing normalized decisions, orders, fills, account rows, trades, policy facts, strategy hash, dataset fingerprint, initial cash, and evaluation dates. Hash canonical JSON content.

- [ ] **Step 4: Implement independent audit checks**

Recompute causal ordering, prices, quantities, fill eligibility, fees, cash, holdings, equity, trade pairs, and metrics from evidence. Do not call the TDR replay implementation from SE.

- [ ] **Step 5: Wire SE audit into atomic publication**

TDR adapts its frames into `ReplayEvidence`; a non-PASS result aborts publication. Serialize the full SE result to `audit.json`.

- [ ] **Step 6: Run tests and commit**

Run: `pytest packages/strategy_evaluator/tests/functional/test_strategy_evaluator.py tests/functional/test_backtest.py -q`

Expected: PASS.

Commit: `feat: audit deterministic replay evidence in SE`

---

### Task 7: Replace the production CLI and preserve a temporary legacy probe

**Files:**
- Modify: `src/czsc_trader/application/backtest_service.py`
- Modify: `src/czsc_trader/cli/main.py`
- Modify: `tests/functional/test_cli_surface.py`
- Modify: `tests/functional/test_backtest.py`

**Interfaces:**
- Produces: `backtest run --strategy --strategy-version --dataset --symbol --asset --start --end --init-cash`.
- Temporary migration interface: `backtest legacy-run` with the old arguments, removed in Task 10.

- [ ] **Step 1: Write failing CLI-surface tests**

Assert `backtest run` requires strategy, version, dataset, dates, and cash. Assert it rejects `--baseline`, `--windows`, `--window`, and `--fee-rate`. Assert JSON output carries the formal strategy reference and final artifact directory.

- [ ] **Step 2: Run CLI tests and confirm old argument behavior**

Run: `pytest tests/functional/test_cli_surface.py tests/functional/test_backtest.py -q`

Expected: FAIL because the old parser is still active.

- [ ] **Step 3: Wire the v2 command handler**

Resolve registered strategy through SM, load the explicitly named dataset, construct the required request, and call `run_backtest_v2`.

- [ ] **Step 4: Isolate the old handler under `legacy-run`**

Keep its imports lazy so no v2 path depends on legacy baselines or windows. Mark the result manifest as `legacy_migration_probe`.

- [ ] **Step 5: Run tests and commit**

Run: `pytest tests/functional/test_cli_surface.py tests/functional/test_backtest.py -q`

Expected: PASS.

Commit: `feat: expose backtest v2 CLI`

---

### Task 8: Migrate SM storage and remove production `configs` dependencies

**Files:**
- Move: `configs/strategies/` to `strategies/`
- Modify: `src/czsc_trader/application/context.py`
- Modify: `src/czsc_trader/application/advice_service.py`
- Modify: `src/czsc_trader/application/strategy_service.py`
- Modify: `src/czsc_trader/application/evaluation_service.py`
- Modify: `src/czsc_trader/application/evaluation_evidence.py`
- Modify: `src/czsc_trader/candidate_evaluation.py`
- Modify: `tests/functional/conftest.py`
- Modify: `tests/functional/test_strategy_lifecycle.py`
- Modify: `tests/functional/test_evaluation.py`

**Interfaces:**
- All production SM access resolves from `<repo>/strategies`.
- Candidate evaluation resolves its incumbent through a formal strategy snapshot or an experiment-contained candidate snapshot, with no `baseline_root` dependency.

- [ ] **Step 1: Update the functional repository fixture to copy `strategies/` and both datasets**

Remove the blanket `configs` copy. Copy only tracked domain assets required by the exercised workflow.

- [ ] **Step 2: Run strategy/evaluation tests and observe old-path failures**

Run: `pytest tests/functional/test_strategy_lifecycle.py tests/functional/test_evaluation.py -q`

Expected: FAIL where code still reads `configs`.

- [ ] **Step 3: Move SM assets and update all consumers**

Use `git mv` for tracked strategy files. Replace legacy payload resolution inside current candidate evaluation with the Task 1 snapshot adapter while preserving experiment artifacts and hashes.

- [ ] **Step 4: Audit remaining production references**

Run: `rg -n "configs[/\\]|baseline_root|execution_policy_root|backtest_windows" src packages`

Expected: only explicitly temporary legacy modules referenced by `legacy-run` remain.

- [ ] **Step 5: Run tests and commit**

Run: `pytest tests/functional/test_strategy_lifecycle.py tests/functional/test_evaluation.py tests/functional/test_data_and_advice.py -q`

Expected: PASS.

Commit: `refactor: move strategy assets out of configs`

---

### Task 9: Validate S001 and candidate parity on real repository data

**Files:**
- Modify: `tests/functional/test_backtest.py`

**Interfaces:**
- Uses: v2 CLI, temporary legacy CLI, formal `S001-v1`, formal `S001-v2`, and one representative candidate snapshot.
- Produces: a migration comparison that identifies every material result difference by semantic cause.

- [ ] **Step 1: Run v2 for S001-v1 on the research dataset through 2026-09-02**

Use 100,000 initial cash and explicit dates. Confirm the manifest identifies `S001-v1`, adjusted signal data, unadjusted execution data, and SE PASS.

- [ ] **Step 2: Run the legacy probe over the same interval**

Compare signal dates and target positions first. Then compare price, quantity, fills, equity, and metrics, classifying differences caused by unadjusted prices, tick rounding, lot sizing, or corrected execution audit.

- [ ] **Step 3: Repeat for S001-v2 and a representative candidate**

Confirm both strategy sources use identical core logic and artifact schemas.

- [ ] **Step 4: Cross-check PTE-visible S001-v1 dates**

Replay through the latest locally available `data/backtest` date and compare signal date, factor score, target position, valid session, action, limit price, and intended quantity against PTE audit events. Treat broker fills as PTE facts and report expected/actual differences explicitly.

- [ ] **Step 5: Add stable parity assertions and commit**

Keep one end-to-end regression that covers the formal strategy path and one that covers the candidate path; avoid preserving every temporary diagnostic case.

Commit: `test: verify backtest v2 migration parity`

---

### Task 10: Retire legacy backtest and clean repository contracts

**Files:**
- Delete: legacy `src/czsc_trader/backtest_runner.py` paths superseded by v2
- Delete or reduce: legacy `src/czsc_trader/backtest.py`, `src/czsc_trader/baselines.py`, `src/czsc_trader/execution_policies.py` when no longer referenced
- Delete: `configs/backtest_windows/`
- Archive then delete: `configs/rule_baselines/`
- Archive then delete: `configs/execution_policies/`
- Remove: temporary `backtest legacy-run`
- Modify: `README.md`
- Modify: `docs/DEVELOPMENT_HANDOFF.md`
- Modify: `docs/RESEARCH_HANDOFF.md`
- Modify: `tests/functional/test_cli_surface.py`
- Modify: `tests/functional/test_backtest.py`

**Interfaces:**
- Final production surface contains only Backtest v2.
- No runtime path reads `configs`.

- [ ] **Step 1: Prove legacy code is unreachable outside its migration command**

Run repository-wide imports and reference searches. Move any historically required JSON into the referencing experiment or `archive/legacy/`, preserving original bytes and hashes.

- [ ] **Step 2: Delete legacy entry and dead modules**

Remove `legacy-run`, old CLI flags, obsolete report labels, and dead adapters. Keep reusable factor, metric, chart, and execution primitives where v2 imports them.

- [ ] **Step 3: Delete the empty `configs` tree**

Run: `rg -n "configs[/\\]|baseline_20|backtest_windows" src packages tests README.md docs`

Expected: no production or current-document references; historical experiment documents may mention their original provenance.

- [ ] **Step 4: Update user and handoff documentation**

Document formal and candidate backtest workflows, dataset update/validation, explicit dates, artifact meanings, SE audit, and PTE cross-check procedure. Keep handoff documents focused on current workflow.

- [ ] **Step 5: Run the minimum complete functional regression**

Run:

```powershell
pytest tests/functional/test_backtest.py `
       tests/functional/test_data_and_advice.py `
       tests/functional/test_evaluation.py `
       tests/functional/test_strategy_lifecycle.py `
       tests/functional/test_cli_surface.py `
       packages/strategy_evaluator/tests/functional/test_strategy_evaluator.py -q
ruff check src packages tests/functional
python -m compileall -q src packages
```

Expected: all tests PASS, Ruff PASS, compilation PASS.

- [ ] **Step 6: Run delivery smoke tests**

Run one formal S001-v1 backtest, open/parse every artifact, validate the two datasets, and run one advice call using the same signal day. Confirm Git status contains only intended source, domain-asset, test, and documentation changes.

- [ ] **Step 7: Commit final cutover**

Commit: `refactor: replace legacy TDR backtest`
