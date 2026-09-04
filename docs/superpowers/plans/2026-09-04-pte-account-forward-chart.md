# PTE Account Forward Chart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an account-scoped forward-observation chart to the PTE console while keeping forward market-data ownership in PTE and making TDR a pure stdin-to-HTML renderer.

**Architecture:** PTE snapshots the immutable strategy selection cutoff, builds a bounded account-observation document from its runtime market data and account ledger, invokes a TDR CLI renderer through stdin/stdout, and atomically caches the returned HTML. TDR validates the versioned document and renders Plotly/CZSC output entirely in memory; the PTE web layer serves chart status and cache files without coupling chart failures to trading health.

**Tech Stack:** Python 3.11+, SQLite, pandas, Plotly, CZSC, `subprocess.run`, vanilla JavaScript, Node test runner, pytest.

**Spec:** `docs/superpowers/specs/2026-09-04-pte-account-forward-chart-design.md`

## Global Constraints

- PTE owns and stores all forward market data; TDR must not read or persist it.
- The renderer contract is `account_observation.v1`, JSON on stdin and self-contained HTML on stdout.
- The default context is exactly 180 complete trading sessions before `selection_data_cutoff`, plus every available later daily bar.
- New virtual accounts require an immutable ISO `selection_data_cutoff`; migrated accounts may remain nullable only until safe hash-matched backfill.
- Renderer limits are 5 MiB input, 20 MiB output, and 30 seconds.
- Chart generation is read-only and must not change scheduler failures, account health, pause state, decisions, orders, fills, or reconciliation.
- Extend the existing consolidated functional test files; do not add fine-grained test files.
- Use no network, Tushare, Futu, formal runtime database, or repository market-data files in tests.

---

### Task 1: Pure TDR Observation Renderer and CLI Contract

**Files:**
- Create: `src/czsc_trader/observation_chart.py`
- Modify: `src/czsc_trader/cli/main.py`
- Test: `tests/functional/test_charting.py`

**Interfaces:**
- Produces: `render_observation_html(payload: object) -> str`
- Produces: `RawCommandOutput(content: str)` for CLI results that must bypass JSON/text serialization.
- Produces: `czsc-trader chart observation --format html`, reading exactly one JSON document from `sys.stdin` and writing only HTML to `sys.stdout`.

- [ ] **Step 1: Write failing renderer and CLI functional tests**

Add one consolidated scenario to `tests/functional/test_charting.py` that builds valid OHLC bars spanning a cutoff, decisions, intents, fills, and snapshots, then asserts:

```python
html = render_observation_html(observation_payload)
assert "选择截止" in html
assert "BUY" in html and "SELL" in html
assert "DEC-BUY" in html and "INT-BUY" in html and "FILL-BUY" in html
assert "目标持仓" in html and "实际持仓" in html
assert "hoverinfo\":\"skip\"" in html or '"hoverinfo":"skip"' in html
```

Invoke `main(["chart", "observation", "--format", "html"])` with monkeypatched stdin/stdout and assert stdout starts with HTML, while malformed scope, duplicate dates, invalid OHLC, non-finite values, unknown top-level keys, and pre-cutoff observation facts raise a validation error. Monkeypatch `Path.open` to fail so the renderer proves it does not read or write files.

- [ ] **Step 2: Run the focused test and confirm red state**

Run: `python -m pytest tests/functional/test_charting.py -q`

Expected: FAIL because `czsc_trader.observation_chart` and the `chart` CLI resource do not exist.

- [ ] **Step 3: Implement strict request validation and Plotly rendering**

Create `observation_chart.py` with these exact entry points and invariants:

```python
CONTRACT_VERSION = "account_observation.v1"
ALLOWED_TOP_LEVEL = {
    "contract_version", "account", "context_sessions", "market_data",
    "decisions", "intents", "orders", "fills", "snapshots",
}

def validate_observation(payload: object) -> dict[str, object]: ...
def render_observation_html(payload: object) -> str: ...
```

Normalize ISO dates, reject unknown top-level keys, require ascending unique daily bars and valid finite OHLC (`low <= open/close <= high`), require integer quantities, enforce account scope where present, and reject every decision/intent/order/fill/snapshot dated on or before the selection cutoff. Reuse the current charting helpers for daily normalization, CZSC pens, missing-session range breaks, and transparent K-line hover points. Render BUY/SELL triangles, hollow intent circles, fill stars, the cutoff line/forward shading, and target/actual position step traces; return `fig.to_html(full_html=True, include_plotlyjs=True)` without filesystem access.

- [ ] **Step 4: Add the raw-output CLI path**

In `cli/main.py`, add:

```python
@dataclass(frozen=True)
class RawCommandOutput:
    content: str

def _run_chart_observation(args: argparse.Namespace) -> RawCommandOutput:
    payload = json.load(sys.stdin)
    return RawCommandOutput(render_observation_html(payload))
```

Register `chart observation --format html`; after dispatch, detect `RawCommandOutput`, write `content` directly, and return before the generic `write_result` path. Diagnostics and parse failures continue to stderr/nonzero through the existing CLI error boundary.

- [ ] **Step 5: Run focused tests and commit**

Run: `python -m pytest tests/functional/test_charting.py -q`

Expected: PASS.

Commit:

```powershell
git add src/czsc_trader/observation_chart.py src/czsc_trader/cli/main.py tests/functional/test_charting.py
git commit -m "feat: add pure account observation renderer"
```

---

### Task 2: Immutable Selection Cutoff in PTE Account Identity

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/store.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/cli.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/audit.py`
- Test: `packages/paper_trading_engine/tests/functional/test_virtual_accounts.py`

**Interfaces:**
- Consumes: existing `strategy show` result with `release_hash` and `selection_data_cutoff`.
- Produces: `PaperStore.create_virtual_account(..., selection_data_cutoff: str)`.
- Produces: `PaperStore.backfill_account_selection_cutoff(account_id: str, release_hash: str, selection_data_cutoff: str) -> bool`, which writes only when the stored hash matches and the current cutoff is null.

- [ ] **Step 1: Write failing migration, creation, and identity tests**

Extend the existing virtual-account scenario to verify:

```python
created = store.create_virtual_account(..., selection_data_cutoff="2026-09-02")
assert created["selection_data_cutoff"] == "2026-09-02"
with pytest.raises(ValueError, match="selection_data_cutoff"):
    store.create_virtual_account(..., selection_data_cutoff="")
with pytest.raises(ValueError, match="immutable"):
    store.create_virtual_account(...same identity..., selection_data_cutoff="2026-09-01")
```

Create a legacy table without the column, reopen it, and assert the nullable column is added. Assert backfill succeeds only for an exact release hash and never overwrites a populated cutoff.

- [ ] **Step 2: Run the PTE account test and confirm red state**

Run: `python -m pytest packages/paper_trading_engine/tests/functional/test_virtual_accounts.py -q`

Expected: FAIL because the account schema and creation signature lack `selection_data_cutoff`.

- [ ] **Step 3: Add schema migration and immutable storage semantics**

Add `selection_data_cutoff TEXT` to new schemas and `_ensure_column` migration. Validate with `date.fromisoformat`, include it in INSERT/select dictionaries and the idempotent immutable identity tuple, and implement hash-matched null-only backfill in one transaction.

- [ ] **Step 4: Pass strategy metadata through creation and safely backfill legacy accounts**

Use the existing `strategy show` CLI response during account creation and require its cutoff. During engine bootstrap, query only accounts missing the cutoff, resolve each stored `strategy_id/strategy_version`, compare `release_hash`, and call `backfill_account_selection_cutoff`. A resolution/hash failure leaves the account running and records one `ACCOUNT_CHART_GENERATION_FAILED` system event with operation `selection_cutoff_backfill`; it must not add a scheduler failure.

- [ ] **Step 5: Run focused tests and commit**

Run: `python -m pytest packages/paper_trading_engine/tests/functional/test_virtual_accounts.py -q`

Expected: PASS.

Commit:

```powershell
git add packages/paper_trading_engine/src/paper_trading_engine/store.py packages/paper_trading_engine/src/paper_trading_engine/cli.py packages/paper_trading_engine/src/paper_trading_engine/audit.py packages/paper_trading_engine/tests/functional/test_virtual_accounts.py
git commit -m "feat: bind selection cutoff to virtual accounts"
```

---

### Task 3: PTE Observation Builder, Renderer Process, and Cache

**Files:**
- Create: `packages/paper_trading_engine/src/paper_trading_engine/account_chart.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/store.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/coordinator.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/cli.py`
- Test: `packages/paper_trading_engine/tests/functional/test_virtual_accounts.py`

**Interfaces:**
- Produces: `AccountChartService.status(account_id: str) -> dict[str, object]` with `scope`, `status`, `selection_data_cutoff`, `context_sessions`, `chart_url`, `fingerprint`, and `message`.
- Produces: `AccountChartService.chart_path(account_id: str) -> Path`, constrained below its configured cache root.
- Consumes: `PaperStore.account_decisions`, `account_intents`, `account_orders`, `account_fills`, and `account_snapshots`.
- Consumes renderer command: `[trader_executable, "chart", "observation", "--format", "html"]`.

- [ ] **Step 1: Write failing service tests with a fake renderer**

Build a temporary runtime manifest/daily CSV and seeded account ledger. Inject a fake runner that captures `input` and returns `CompletedProcess(..., returncode=0, stdout="<!doctype html>...", stderr="")`. Assert the request contains exactly 180 pre-cutoff sessions plus all later bars, only the selected account facts, `adjustment == "hfq"`, and a SHA-256 manifest identity. Assert a second call reuses the cache without a second runner call; changing a decision changes the fingerprint and regenerates.

Also assert timeout, nonzero exit, oversized input/output, malformed HTML, missing cutoff, missing bars, and unsafe account IDs produce `UNAVAILABLE`/`EMPTY` without mutating scheduler or account state. Assert repeated identical failure emits one failure event and subsequent success emits one recovery event.

- [ ] **Step 2: Run the focused PTE test and confirm red state**

Run: `python -m pytest packages/paper_trading_engine/tests/functional/test_virtual_accounts.py -q`

Expected: FAIL because `AccountChartService` does not exist.

- [ ] **Step 3: Implement bounded request construction**

Create `account_chart.py` with:

```python
class AccountChartService:
    def __init__(self, store, data_dir: Path, cache_dir: Path,
                 trader_executable: str = "czsc-trader", runner=subprocess.run,
                 timeout_seconds: int = 30, context_sessions: int = 180): ...
    def status(self, account_id: str) -> dict[str, object]: ...
    def chart_path(self, account_id: str) -> Path: ...
```

Read and hash the manifest bytes, select only the listed daily HFQ files for the account symbol, parse them in memory, de-duplicate/sort, and slice `<= cutoff` to the last 180 plus every `> cutoff`. Normalize ledger rows into the request and exclude every observation fact on or before the cutoff. Serialize with deterministic separators and `sort_keys=True`; reject input larger than `5 * 1024 * 1024` bytes.

- [ ] **Step 4: Implement per-account generation, atomic cache, and isolated audit**

Hash the canonical request to form the fingerprint. Under a per-account `threading.Lock`, compare `observation.meta.json`; on a hit return the existing HTML. On a miss run the exact renderer command with `input=request_json`, `text=True`, `capture_output=True`, `shell=False`, and `timeout=30`; require a zero exit, output no larger than 20 MiB, and an HTML doctype/tag. Write HTML and metadata to unique sibling temporary files, `os.replace` each into place, and remove leftovers in `finally`.

Use system events `ACCOUNT_CHART_GENERATION_FAILED` and `ACCOUNT_CHART_RECOVERED`. Store the last error fingerprint per account in cache metadata so identical failures are deduplicated across requests and restarts. Keep these events outside scheduler failures and account-health inputs.

- [ ] **Step 5: Wire the service into engine/coordinator and commit**

Construct the service from the same PTE runtime `data_dir`, with cache root `state/paper_trading/charts`, and expose it as `PteCoordinator.account_chart`. Run:

`python -m pytest packages/paper_trading_engine/tests/functional/test_virtual_accounts.py -q`

Expected: PASS.

Commit:

```powershell
git add packages/paper_trading_engine/src/paper_trading_engine/account_chart.py packages/paper_trading_engine/src/paper_trading_engine/store.py packages/paper_trading_engine/src/paper_trading_engine/coordinator.py packages/paper_trading_engine/src/paper_trading_engine/cli.py packages/paper_trading_engine/tests/functional/test_virtual_accounts.py
git commit -m "feat: build and cache account observation charts"
```

---

### Task 4: Account-Scoped HTTP and Console Interaction

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/web_api.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/web.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/static/app.js`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/static/styles.css`
- Test: `packages/paper_trading_engine/tests/functional/test_web_console.py`
- Test: `packages/paper_trading_engine/tests/functional/console_state.test.mjs`

**Interfaces:**
- Consumes: `PteCoordinator.account_chart.status(account_id)` and `.chart_path(account_id)`.
- Produces: `GET /api/virtual-accounts/{account_id}/chart`.
- Produces: `GET /charts/{account_id}/observation.html?v={fingerprint}` with ETag.
- Produces JS: `chartShouldReload(previousScope, nextStatus) -> boolean` and account-scoped chart loading through the existing `ScopedLoader`.

- [ ] **Step 1: Write failing web and browser-state tests**

Extend `test_web_console.py` with a fake chart service and assert account A/B return only their own scope and cache file. Assert `..`, encoded separators, unknown accounts, and mismatched fingerprint cannot leave the configured cache root. Assert chart HTML returns `Content-Type: text/html`, ETag/conditional 304, while JSON APIs remain `Cache-Control: no-store`.

Extend `console_state.test.mjs`:

```javascript
assert.equal(chartShouldReload(null, {scope:{account_id:'a'}, fingerprint:'f1'}), true);
assert.equal(chartShouldReload({account_id:'a', fingerprint:'f1'}, {scope:{account_id:'a'}, fingerprint:'f1'}), false);
assert.equal(chartShouldReload({account_id:'a', fingerprint:'f1'}, {scope:{account_id:'b'}, fingerprint:'f1'}), true);
```

- [ ] **Step 2: Run focused tests and confirm red state**

Run:

```powershell
python -m pytest packages/paper_trading_engine/tests/functional/test_web_console.py -q
node --test packages/paper_trading_engine/tests/functional/console_state.test.mjs
```

Expected: FAIL because the routes and JS chart state do not exist.

- [ ] **Step 3: Add safe API and HTML serving routes**

Add `PteWebApi.virtual_account_chart(account_id)` delegating to the chart service. Route the two exact URL shapes in `web.py`; decode and validate account IDs against `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, resolve only `account_chart.chart_path(account_id)`, and require the requested fingerprint to match status metadata. Return ETag and `Cache-Control: private, max-age=31536000, immutable` for fingerprinted HTML; honor `If-None-Match`. Preserve `no-store` for operational JSON.

- [ ] **Step 4: Add the chart card and stable iframe lifecycle**

Insert “前瞻观察” after “最新决策” in `renderAccount`. Render a scoped skeleton immediately, then request `/api/virtual-accounts/${encodeURIComponent(accountId)}/chart`. Before applying a response, verify the route still selects that account and the loader token remains current. For `READY`, set the iframe URL only when account or fingerprint changes; for `EMPTY` and `UNAVAILABLE`, render the supplied message and a retry button. Add the fixed governance copy and identity badge, plus responsive 760px iframe styling. Account polling must leave the iframe DOM node untouched when the fingerprint is unchanged.

- [ ] **Step 5: Run focused tests and commit**

Run the two commands from Step 2; expected: PASS.

Commit:

```powershell
git add packages/paper_trading_engine/src/paper_trading_engine/web_api.py packages/paper_trading_engine/src/paper_trading_engine/web.py packages/paper_trading_engine/src/paper_trading_engine/static/app.js packages/paper_trading_engine/src/paper_trading_engine/static/styles.css packages/paper_trading_engine/tests/functional/test_web_console.py packages/paper_trading_engine/tests/functional/console_state.test.mjs
git commit -m "feat: show forward chart on account console"
```

---

### Task 5: Documentation, Regression, Restart, and Delivery Evidence

**Files:**
- Modify: `README.md`
- Modify: `packages/paper_trading_engine/README.md`
- Modify: `docs/DEVELOPMENT_HANDOFF.md`
- Modify: `docs/RESEARCH_HANDOFF.md`

**Interfaces:**
- Documents: account chart purpose, 180-session context, strategy-bound cutoff, PTE/TDR ownership, cache recovery, and data-pollution interpretation.

- [ ] **Step 1: Update operator and handoff documentation**

Document that the chart is read-only, left of the red line is contextual history, right is forward observation, and inspecting data does not retroactively make it out-of-sample; each strategy version carries its own cutoff. Document the two HTTP endpoints, local cache location, retry behavior, and the invariant that TDR receives data through stdin and persists none.

- [ ] **Step 2: Run formatting/static checks available in the repository**

Run:

```powershell
python -m compileall -q src packages/paper_trading_engine/src
node --check packages/paper_trading_engine/src/paper_trading_engine/static/app.js
```

Expected: both exit 0.

- [ ] **Step 3: Run consolidated functional regression**

Run:

```powershell
python -m pytest tests/functional -q
python -m pytest packages/strategy_manager/tests/functional -q
python -m pytest packages/strategy_evaluator/tests/functional -q
python -m pytest packages/paper_trading_engine/tests/functional -q
node --test packages/paper_trading_engine/tests/functional/console_state.test.mjs
```

Expected: every command exits 0. Record exact counts and durations in the delivery response.

- [ ] **Step 4: Commit documentation and any verification-only corrections**

```powershell
git add README.md packages/paper_trading_engine/README.md docs/DEVELOPMENT_HANDOFF.md docs/RESEARCH_HANDOFF.md
git commit -m "docs: explain account forward observation charts"
```

- [ ] **Step 5: Peacefully restart PTE and perform live delivery verification**

Use the existing PTE restart command/IPC path. Verify `http://127.0.0.1:8080/` responds, both virtual accounts return distinct chart scopes, each chart HTML loads, repeated status reads retain the fingerprint, account switching does not reload an unchanged iframe, and a forced fake renderer failure in an isolated temporary instance does not affect the running service.

- [ ] **Step 6: Review branch and request integration authorization**

Run:

```powershell
git status --short --branch
git log --oneline master..HEAD
git diff --stat master...HEAD
```

Expected: clean feature branch with the planned commits. Summarize live evidence and ask the user whether to merge `codex/pte-account-chart` into `master` and push.
