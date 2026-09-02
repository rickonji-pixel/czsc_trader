# PTE Virtual Accounts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the independently selectable execution policy with one complete immutable baseline and add isolated PTE virtual accounts that can run, settle, observe, and compare frozen strategies alongside the single Futu simulation channel.

**Architecture:** `czsc_trader` owns a complete baseline and emits `advice.v3`; PTE consumes that contract through CLI and persists one ledger per virtual account. A coordinator isolates Futu channel failures from virtual settlement, while the existing local HTTP process exposes both channel and virtual-account state.

**Tech Stack:** Python 3.11+, dataclasses, argparse, pandas, SQLite, `http.server`, pytest, HTML/CSS/JavaScript.

**Spec:** `docs/superpowers/specs/2026-09-03-pte-virtual-accounts-design.md`

## Global Constraints

- Develop on `codex/pte-virtual-accounts`; do not use a git worktree or subagent.
- Preserve `baseline_20260901`, `execution_policy_20260902`, and `experiments/0902_EX02` byte-for-byte.
- Production runtime selects exactly one complete baseline; it must not resolve `configs/execution_policies/registry.json`.
- PTE remains independent and calls `czsc-trader` through CLI; it must not import `czsc_trader.*`.
- Keep Futu hard-locked to `TrdEnv.SIMULATE`, market `CN`, symbol `588080.SH`, broker price adjustment disabled, and explicit-fill reconciliation.
- Use 100-share lots, price tick `0.001`, price-limit ratio `0.2`, fee rate `0.0005`, and initial virtual cash `1_000_000.00`.
- Only a confirmed fill changes cash or holdings. Pausing blocks new orders and preserves reconciliation.
- Use the repository development pool through 2026-09-02 for the execution audit; do not rewrite prior experiment artifacts.
- Run focused tests, Python compilation, and `git diff --check`; full slow end-to-end tests remain outside the default scope.

---

### Task 1: Parse and register a complete immutable baseline

**Files:**
- Modify: `src/czsc_trader/baselines.py`
- Create: `configs/rule_baselines/baseline_20260903.json`
- Modify: `configs/rule_baselines/registry.json`
- Modify: `tests/test_repository_contract.py`
- Modify: `tests/test_identity_and_archives.py`

**Interfaces:**
- Produces: `ExecutionSpec`, `InstrumentSpec`, `CapitalSpec`, `VirtualFillSpec` frozen dataclasses.
- Produces: `ResolvedBaseline.execution: ExecutionSpec | None`.
- Produces: active `baseline_20260903`, containing candidate 143 signal parameters plus the reviewed `execution` object.

- [ ] **Step 1: Write failing complete-baseline parser tests**

Add tests that resolve `baseline_20260903` and assert:

```python
baseline = resolve_baseline(REPO_ROOT / "configs" / "rule_baselines")
assert baseline.version == "baseline_20260903"
assert baseline.rule_payload["candidate_id"] == 143
assert baseline.execution is not None
assert baseline.execution.instrument.symbol == "588080.SH"
assert baseline.execution.instrument.price_tick == pytest.approx(0.001)
assert baseline.execution.instrument.lot_size == 100
assert baseline.execution.instrument.price_limit_ratio == pytest.approx(0.2)
assert baseline.execution.capital.target_scope == "entry_cycle"
assert baseline.execution.virtual_fill.touch_only == "uncertain_unfilled"
```

Add malformed temporary registries covering a missing execution field, unknown enum, non-positive tick, non-100 lot, and differing exit/price-limit ratios.

- [ ] **Step 2: Run the parser tests and confirm failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_repository_contract.py tests\test_identity_and_archives.py -q
```

Expected: failures because the complete execution dataclasses and `baseline_20260903` do not exist.

- [ ] **Step 3: Implement strict execution parsing**

Add frozen dataclasses and `_parse_execution(payload, baseline_symbol)` to `baselines.py`. Validate every enum and numeric field, including:

```python
if execution.exit.limit_ratio != execution.instrument.price_limit_ratio:
    raise ValueError("baseline exit ratio differs from instrument price limit ratio")
if execution.instrument.lot_size != 100:
    raise ValueError("complete baseline requires 100-share lots")
if execution.capital.target_scope != "entry_cycle":
    raise ValueError("complete baseline capital target must be entry-cycle scoped")
```

Archived baselines may resolve with `execution=None`; the active baseline must have a complete execution object.

- [ ] **Step 4: Create and register the immutable complete baseline**

Copy the signal portion of `baseline_20260901.json`, change `schema_version` to `2`, remove the legacy string field `"execution": "next_session_open"`, and add the reviewed structured execution object from the spec. Register the canonical SHA-256, mark `baseline_20260901` archived, and set `baseline_20260903` active/latest. Record source baseline identity and `execution_review_end: "2026-09-02"` in the registry without changing the archived file.

- [ ] **Step 5: Run tests and commit**

Run the Task 1 test command. Expected: PASS.

```powershell
git add src/czsc_trader/baselines.py configs/rule_baselines tests/test_repository_contract.py tests/test_identity_and_archives.py
git commit -m "feat: bundle execution rules into active baseline"
```

---

### Task 2: Publish advice.v3 with stable entry-cycle sizing

**Files:**
- Modify: `src/czsc_trader/application/advice_service.py`
- Modify: `src/czsc_trader/application/context.py`
- Modify: `src/czsc_trader/cli/main.py`
- Modify: `tests/test_execution_policy.py`
- Modify: `tests/test_cli_e2e.py`

**Interfaces:**
- Consumes: `ResolvedBaseline.execution` from Task 1.
- Produces: `AdviceCommand.cycle_target_quantity: int | None`.
- Produces: `build_advice_v3(..., cycle_target_quantity: int | None) -> dict[str, object]`.
- Produces: CLI option `czsc-trader advice run --cycle-target-quantity`.

- [ ] **Step 1: Write failing advice.v3 contract tests**

Cover first entry, retry, holding, exit, cash, deterministic decision identity, order splitting, and the regression where price improvement leaves enough cash for another lot:

```python
first = build_advice_v3(
    baseline=baseline, signal_date=pd.Timestamp("2026-09-01"),
    valid_session=pd.Timestamp("2026-09-02"), signal_close=1.704,
    execution_close=1.688, target_position=1, actual_quantity=0,
    available_cash=1_000_000.0, cycle_target_quantity=None,
)
retry = build_advice_v3(
    baseline=baseline, signal_date=pd.Timestamp("2026-09-02"),
    valid_session=pd.Timestamp("2026-09-03"), signal_close=1.68,
    execution_close=1.65, target_position=1, actual_quantity=first["target_quantity"],
    available_cash=10_000.0, cycle_target_quantity=first["target_quantity"],
)
assert first["contract_version"] == "advice.v3"
assert "execution_policy" not in first
assert retry["action"] == "HOLD"
assert retry["order"] is None
```

Assert each order quantity is at most 1,000,000 and all split quantities sum to the remaining delta.

- [ ] **Step 2: Run advice tests and confirm failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_execution_policy.py tests\test_cli_e2e.py -q -k "advice or complete_baseline"
```

Expected: failure because `build_advice_v3` and the CLI argument are absent.

- [ ] **Step 3: Implement advice.v3**

Move price, fee, lot and maximum-quantity inputs to `baseline.execution`. Compute the cycle target only when target position is 1 and no cycle target exists. For an existing target, calculate only the remaining delta. Return `orders: list[dict]` so oversized quantities are split deterministically; retain `order` as the sole item or `None` for convenient PTE consumption.

Use `Decimal(str(value))` for money and preserve project-owned prices:

```python
lots = (cash / (unit_cost * lot_size)).to_integral_value(rounding=ROUND_FLOOR)
new_target = int(lots) * lot_size
target = new_target if cycle_target_quantity is None else cycle_target_quantity
delta = max(0, target - actual_quantity)
```

On target position 0, sell the entire actual quantity and return `cycle_target_quantity=0` only after the caller reports zero actual holdings.

- [ ] **Step 4: Remove production policy resolution**

Delete `resolve_execution_policy` use from `run_advice`; remove `execution_policy_root` from the application context when no production caller remains. Preserve `src/czsc_trader/execution_policies.py` for historical verification only. Extend the CLI handler to pass `--cycle-target-quantity`.

- [ ] **Step 5: Run tests and commit**

Run Task 2 tests plus:

```powershell
.\.venv\Scripts\czsc-trader.exe advice run --repo-root . --data-dir data/raw --symbol 588080.SH --asset etf --actual-quantity 0 --available-cash 1000000 --baseline baseline_20260903
```

Expected: PASS JSON with `contract_version=advice.v3`, complete baseline identity, no independent execution-policy identity, and no order for the current cash target if the latest signal is cash.

```powershell
git add src/czsc_trader/application src/czsc_trader/cli tests/test_execution_policy.py tests/test_cli_e2e.py
git commit -m "feat: publish stable advice v3 decisions"
```

---

### Task 3: Re-audit execution semantics on the development pool

**Files:**
- Modify: `src/czsc_trader/execution_policy.py`
- Create: `experiments/0903_EX01/01_goal.md`
- Create: `experiments/0903_EX01/02_design.md`
- Create: `experiments/0903_EX01/03_execution.md`
- Create: `experiments/0903_EX01/04_conclusion.md`
- Create: `experiments/0903_EX01/run_experiment.py`
- Create: `experiments/0903_EX01/artifacts/execution_review.json`
- Create: `experiments/0903_EX01/experiment_manifest.json`
- Modify: `tests/test_execution_policy.py`
- Modify: `tests/test_identity_and_archives.py`

**Interfaces:**
- Produces: lot-aware `simulate_limit_policy(..., lot_size: int = 100, touch_only_fills: bool = False)`.
- Produces: immutable audit `execution_review.json` for `baseline_20260903` through 2026-09-02.

- [ ] **Step 1: Write failing conservative-fill and integer-lot tests**

Add deterministic fixtures asserting:

```python
assert result.orders.iloc[0]["size"] % 100 == 0
assert result.daily_state.iloc[-1]["cash"] >= 0
assert equal_touch_result.orders.empty
assert penetrated_result.orders.iloc[0]["trigger"] == "intraday_limit"
```

Also assert an entry-cycle target remains fixed across retry dates.

- [ ] **Step 2: Run focused tests and confirm failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_execution_policy.py -q -k "lot or touch or cycle"
```

Expected: failures because the historical simulator uses fractional shares and accepts equal touches.

- [ ] **Step 3: Implement reviewed simulation semantics**

Round affordable shares down to `lot_size`, require intraday low to be strictly below the limit when `touch_only_fills=False`, retain non-negative cash, and close only the held integer quantity. Keep the default arguments explicit so old callers are updated rather than silently changing archived results.

- [ ] **Step 4: Create and run 0903_EX01**

The experiment loads only tracked data through 2026-09-02, resolves `baseline_20260903`, runs its single embedded execution rule, and compares:

- legacy fractional/equal-touch semantics;
- reviewed integer-lot/strict-penetration semantics;
- unconditional next-open reference.

Write fill rates, waits, return, maximum drawdown, Calmar ratio, win/loss ratio, cash residuals, uncertain-touch count, and identity hashes. The experiment is an audit, not a candidate search, and must not alter the embedded parameter.

Run:

```powershell
.\.venv\Scripts\python.exe experiments\0903_EX01\run_experiment.py
```

Expected: `status=COMPLETE`, candidate 143 identity unchanged, all order sizes integral 100-share lots, and a recorded conclusion on the reviewed rule.

- [ ] **Step 5: Validate archive and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_execution_policy.py tests\test_identity_and_archives.py -q
git add src/czsc_trader/execution_policy.py tests experiments/0903_EX01
git commit -m "research: audit complete baseline execution semantics"
```

---

### Task 4: Consume advice.v3 from PTE

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/contracts.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/advice_client.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/engine.py`
- Modify: `packages/paper_trading_engine/tests/test_advice_client.py`
- Modify: `packages/paper_trading_engine/tests/test_engine.py`

**Interfaces:**
- Produces: `TradingDecision.cycle_target_quantity: int` and `TradingDecision.orders: tuple[OrderInstruction, ...]`.
- Produces: `CliAdviceClient.get_decision(actual_quantity, available_cash, cycle_target_quantity=None, baseline=None)`.

- [ ] **Step 1: Write failing PTE contract tests**

Update fixtures to `advice.v3`, remove `execution_policy`, and assert malformed split orders, baseline identity, lot size, DAY time-in-force and numeric limits are rejected. Assert CLI arguments include both explicit `--baseline` and optional `--cycle-target-quantity`.

- [ ] **Step 2: Run PTE contract tests and confirm failure**

```powershell
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests\test_advice_client.py packages\paper_trading_engine\tests\test_engine.py -q
```

Expected: failures against the advice.v2 parser and single-order assumptions.

- [ ] **Step 3: Implement advice.v3 parsing and client invocation**

Validate that all split orders use the same side and limit, each quantity is a positive 100-share multiple, and the sum equals `abs(delta_quantity)`. Pass the requested baseline explicitly on every CLI call.

- [ ] **Step 4: Adapt the Futu engine without weakening reconciliation**

Persist the returned cycle target in store settings for the single Futu account, submit split intents one at a time under existing active-order protection, and clear the target after confirmed full exit. Preserve intent-before-submit, remark reconciliation and monotonic cumulative fills.

- [ ] **Step 5: Run tests and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests\test_advice_client.py packages\paper_trading_engine\tests\test_engine.py packages\paper_trading_engine\tests\test_futu_gateway.py -q
git add packages/paper_trading_engine/src/paper_trading_engine packages/paper_trading_engine/tests
git commit -m "feat: consume complete baseline decisions in PTE"
```

---

### Task 5: Add isolated virtual-account storage

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/store.py`
- Create: `packages/paper_trading_engine/src/paper_trading_engine/virtual_models.py`
- Create: `packages/paper_trading_engine/tests/test_virtual_store.py`

**Interfaces:**
- Produces: `VirtualAccount`, `VirtualOrder`, `VirtualFill`, `VirtualSnapshot` dataclasses.
- Produces store methods `create_virtual_account`, `virtual_accounts`, `virtual_account`, `set_virtual_paused`, `save_virtual_intent`, `upsert_virtual_order`, `settle_virtual_order`, `save_virtual_snapshot`.

- [ ] **Step 1: Write failing SQLite isolation and transaction tests**

Create two accounts with the same decision ID and assert both can persist independent intents. Verify duplicate account decisions are idempotent, duplicate fill sequences do not change balances, failed settlement rolls back, paused state is per-account, and reopening the database restores cycle targets.

- [ ] **Step 2: Run store tests and confirm failure**

```powershell
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests\test_virtual_store.py -q
```

Expected: import or attribute failures because virtual storage is absent.

- [ ] **Step 3: Add virtual tables and dataclasses**

Create the five tables and unique constraints in the specification. Store money as integer ten-thousandths of a yuan or canonical decimal strings; convert through `Decimal` at boundaries. Use one SQLite transaction for fill insertion, order status, cash, holdings, average cost and snapshot updates.

- [ ] **Step 4: Run tests and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests\test_virtual_store.py packages\paper_trading_engine\tests\test_engine.py -q
git add packages/paper_trading_engine/src/paper_trading_engine/store.py packages/paper_trading_engine/src/paper_trading_engine/virtual_models.py packages/paper_trading_engine/tests/test_virtual_store.py
git commit -m "feat: persist isolated virtual account ledgers"
```

---

### Task 6: Implement deterministic virtual settlement and coordination

**Files:**
- Create: `packages/paper_trading_engine/src/paper_trading_engine/virtual_fill.py`
- Create: `packages/paper_trading_engine/src/paper_trading_engine/virtual_engine.py`
- Create: `packages/paper_trading_engine/src/paper_trading_engine/coordinator.py`
- Create: `packages/paper_trading_engine/tests/test_virtual_fill.py`
- Create: `packages/paper_trading_engine/tests/test_virtual_engine.py`
- Create: `packages/paper_trading_engine/tests/test_coordinator.py`

**Interfaces:**
- Produces: `settle_day_order(order, bar, execution) -> FillOutcome`.
- Produces: `VirtualAccountEngine.refresh(account_id, published_session) -> dict[str, object]`.
- Produces: `PteCoordinator` implementing the web operations protocol for channel plus virtual accounts.

- [ ] **Step 1: Write failing fill-model tests**

Cover open improvement, strict intraday penetration, equal-touch uncertainty, no touch, sell-at-open, invalid OHLC, fees, round lots and deterministic repeated settlement.

- [ ] **Step 2: Write failing virtual-engine and coordinator tests**

Use fake advice and bars to prove two accounts remain isolated, only explicit fills change holdings, a paused account still settles an existing order, restart is idempotent, one broken virtual account does not block another, and a failing Futu engine does not block virtual refresh.

- [ ] **Step 3: Run new tests and confirm failure**

```powershell
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests\test_virtual_fill.py packages\paper_trading_engine\tests\test_virtual_engine.py packages\paper_trading_engine\tests\test_coordinator.py -q
```

Expected: module import failures.

- [ ] **Step 4: Implement fill model, engine and coordinator**

Keep `virtual_fill.py` pure. Make `virtual_engine.py` perform settlement before requesting the next decision, and use store uniqueness for idempotency. Make `coordinator.py` catch failures per domain, publish channel and account health separately, and expose combined status without turning channel degradation into a virtual-account failure.

- [ ] **Step 5: Run tests and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests\test_virtual_fill.py packages\paper_trading_engine\tests\test_virtual_engine.py packages\paper_trading_engine\tests\test_coordinator.py -q
git add packages/paper_trading_engine/src/paper_trading_engine packages/paper_trading_engine/tests
git commit -m "feat: run deterministic virtual account simulation"
```

---

### Task 7: Isolate scheduler failures and add bounded backoff

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/scheduler.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/store.py`
- Modify: `packages/paper_trading_engine/tests/test_scheduler.py`

**Interfaces:**
- Produces: per-operation retry state with capped delays `5, 15, 30, 60, 300` seconds.
- Produces: aggregated events with `failure_count`, first/last occurrence and one recovery event.

- [ ] **Step 1: Write failing isolation and event-deduplication tests**

Use a fake clock and a channel refresh that always raises. Assert virtual refresh continues, the failing operation is skipped until due, 100 scheduler ticks produce bounded event rows, and recovery resets the delay and emits one recovery event.

- [ ] **Step 2: Run scheduler tests and confirm failure**

```powershell
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests\test_scheduler.py -q
```

Expected: failures because the current outer loop records an event every 0.5 seconds.

- [ ] **Step 3: Implement per-operation guards and capped backoff**

Replace the all-or-nothing tick with guarded account, order, virtual, decision and publication operations. Store one active failure record per operation and error fingerprint; update its count instead of appending identical events inside the retry window.

- [ ] **Step 4: Run tests and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests\test_scheduler.py packages\paper_trading_engine\tests\test_engine.py packages\paper_trading_engine\tests\test_coordinator.py -q
git add packages/paper_trading_engine/src/paper_trading_engine/scheduler.py packages/paper_trading_engine/src/paper_trading_engine/store.py packages/paper_trading_engine/tests/test_scheduler.py
git commit -m "fix: isolate PTE failures with bounded backoff"
```

---

### Task 8: Add account commands and bootstrap candidate 143

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/cli.py`
- Modify: `packages/paper_trading_engine/tests/test_cli.py`

**Interfaces:**
- Produces: `pte account list|create|pause|resume`.
- Produces: startup `PteCoordinator` containing the existing Futu engine and virtual engine.

- [ ] **Step 1: Write failing CLI tests**

Assert account creation requires a positive decimal cash amount, rejects duplicate IDs, validates the complete baseline through CLI before persistence, and pause/resume target only the selected virtual account.

- [ ] **Step 2: Run CLI tests and confirm failure**

```powershell
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests\test_cli.py -q
```

Expected: parser failures because the account subcommands do not exist.

- [ ] **Step 3: Implement account CLI and runtime assembly**

Build both engines over one `PaperStore`, pass the active baseline explicitly, and preserve existing `pte once` and `pte serve` commands. Add an idempotent bootstrap during serve startup equivalent to:

```powershell
pte account create --repo-root . --account-id baseline-143 --name 候选143 --baseline baseline_20260903 --initial-cash 1000000
```

If the account exists with the same immutable identity, return it unchanged; if the identity differs, stop with an error.

- [ ] **Step 4: Run tests and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests\test_cli.py packages\paper_trading_engine\tests\test_service_config.py packages\paper_trading_engine\tests\test_watchdog.py -q
git add packages/paper_trading_engine/src/paper_trading_engine/cli.py packages/paper_trading_engine/tests/test_cli.py
git commit -m "feat: manage virtual accounts from PTE CLI"
```

---

### Task 9: Extend the HTTP console and dashboard

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/web.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/dashboard.py`
- Modify: `packages/paper_trading_engine/tests/test_web.py`

**Interfaces:**
- Produces: combined `/api/status` containing `channel`, `virtual_accounts`, `selected_account`, and `comparison`.
- Produces: `POST /api/virtual-accounts/{account_id}/pause` and `/resume`.

- [ ] **Step 1: Write failing HTTP tests**

Assert combined status schema, URL-decoded account IDs, per-account pause/resume, 404 for unknown accounts, JSON content type enforcement, and unchanged two-step Futu cancellation.

- [ ] **Step 2: Run web tests and confirm failure**

```powershell
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests\test_web.py -q
```

Expected: failures because virtual endpoints and response sections are absent.

- [ ] **Step 3: Implement API routes and dashboard**

Render four sections from the specification. Use semantic Chinese labels, DOM text nodes, tables and definition lists for structured content. Do not inject untrusted values through `innerHTML`. Compute and label the common observation range; show maximum drawdown, Calmar ratio and win/loss ratio before return. Use a per-account switch whose disabled state reflects only a pending request.

- [ ] **Step 4: Run tests and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests\test_web.py -q
git add packages/paper_trading_engine/src/paper_trading_engine/web.py packages/paper_trading_engine/src/paper_trading_engine/dashboard.py packages/paper_trading_engine/tests/test_web.py
git commit -m "feat: observe and control virtual accounts"
```

---

### Task 10: Update handoff docs and verify the running system

**Files:**
- Modify: `README.md`
- Modify: `docs/DEVELOPMENT_HANDOFF.md`
- Modify: `docs/RESEARCH_HANDOFF.md`
- Modify: `packages/paper_trading_engine/README.md`
- Modify: `tests/test_repository_contract.py`

**Interfaces:**
- Documents: complete-baseline lifecycle, virtual-account commands, 19:00 settlement order, Futu channel role, account comparison and recovery.

- [ ] **Step 1: Update documentation and repository contracts**

Document that future Range candidates become complete baselines before observation and that no independent execution-policy choice exists. Include exact install, start, account list/create/pause/resume, status URL, watchdog and stop commands.

- [ ] **Step 2: Run focused automated verification**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_repository_contract.py tests\test_execution_policy.py tests\test_cli_e2e.py packages\paper_trading_engine\tests -q
.\.venv\Scripts\python.exe -m compileall -q src packages\paper_trading_engine\src experiments\0903_EX01
git diff --check
```

Expected: all selected tests pass, compilation exits zero, and diff check is clean.

- [ ] **Step 3: Perform a local smoke test without submitting a Futu order**

Stop the current service through its documented service control, start the new PTE on port 8080, and verify:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/api/status | ConvertTo-Json -Depth 8
```

Expected: candidate 143 virtual account exists with `1,000,000.00` initial cash, the complete baseline identity is `baseline_20260903`, Futu health is independently reported, and no duplicate virtual order or fill appears after two refreshes. Because the current signal is cash, the smoke test must not submit an order.

- [ ] **Step 4: Inspect the dashboard**

Open `http://127.0.0.1:8080/` and verify the channel section, virtual-account card, Chinese states, formatted structured details, account switch and common-period metric labels render at desktop width. Capture any defect as a focused test before fixing it.

- [ ] **Step 5: Commit the verified delivery**

```powershell
git add README.md docs packages/paper_trading_engine/README.md tests/test_repository_contract.py
git commit -m "docs: hand off complete baseline virtual trading"
git status --short --branch
```

Expected: clean `codex/pte-virtual-accounts` branch containing only reviewed commits. Do not merge to master or push until the user explicitly authorizes those actions.
