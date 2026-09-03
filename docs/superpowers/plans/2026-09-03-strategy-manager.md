# Strategy Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a lightweight Strategy Manager package that gives every strategy a stable identity, immutable versions, qualification lifecycle, audit history, and cross-stage performance evidence while keeping Trader as the only user-facing entry point.

**Architecture:** Add `packages/strategy_manager` as a dependency-free domain package backed by Git-tracked JSON and JSONL files under `configs/strategies`. CZSC Trader owns the CLI façade and publishes `advice.v4`; PTE remains independent and consumes the machine contract without importing Strategy Manager.

**Tech Stack:** Python 3.12, dataclasses, `json`, `hashlib`, `pathlib`, argparse, SQLite in PTE, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-03-strategy-manager-design.md`

## Global Constraints

- Work on `codex/strategy-manager`; do not use a worktree.
- Do not merge to `master` or push without explicit user authorization.
- `strategy_manager` must not import `czsc_trader`, `paper_trading_engine`, broker SDKs, pandas, or vectorbt.
- Trader is the only user-facing Strategy Manager CLI.
- PTE must not import `strategy_manager`; it consumes Trader JSON only.
- Frozen version files and lifecycle/evidence history are Git-tracked and append-only by domain rule.
- Lifecycle qualification and runtime process state remain separate.
- Historical experiments, baseline files, `advice.v3` records, and PTE ledgers are preserved.
- New runtime decisions use `advice.v4` after the atomic cutover.
- Tests are written and observed failing before production implementation.

---

### Task 1: Strategy Manager domain package

**Status:** Complete (`338631a`)

**Files:**
- Create: `packages/strategy_manager/pyproject.toml`
- Create: `packages/strategy_manager/src/strategy_manager/__init__.py`
- Create: `packages/strategy_manager/src/strategy_manager/models.py`
- Create: `packages/strategy_manager/src/strategy_manager/validation.py`
- Create: `packages/strategy_manager/src/strategy_manager/errors.py`
- Create: `packages/strategy_manager/tests/test_models.py`
- Modify: `README.md`

**Interfaces:**
- Produces: `Strategy`, `StrategyVersion`, `LifecycleEvent`, `PerformanceEvidence`, `Qualification`, `canonical_sha256(payload)`, and `validate_*` functions.
- All models expose `from_dict(value)` and `to_dict()` and reject unknown or malformed identity fields.

- [ ] **Step 1: Write failing model and validation tests**

```python
def test_frozen_version_has_stable_release_identity():
    payload = {
        "schema_version": 1,
        "strategy_id": "S001",
        "version": "v1",
        "release_id": "S001-v1",
        "parent_version": None,
        "change_summary": "首个冻结版本",
        "source_experiment": "experiments/0901_EX20",
        "source_candidate": 143,
        "selection_data_cutoff": "2026-09-02",
        "forward_start": "2026-09-03",
        "strategy_payload": {"symbol": "588080"},
    }
    payload["release_hash"] = canonical_sha256(payload)
    version = StrategyVersion.from_dict(payload)
    assert version.release_id == "S001-v1"
    assert version.release_hash == canonical_sha256(version.release_payload())

def test_strategy_id_and_version_are_strict():
    with pytest.raises(ValidationError):
        Strategy.from_dict({
            "schema_version": 1, "strategy_id": "baseline-143", "name": "测试策略",
            "objective": "测试", "responsibility": "测试", "scope": ["588080"],
            "created_at": "2026-09-03T10:00:00+08:00", "created_by": "tester",
        })
    with pytest.raises(ValidationError):
        StrategyVersion.from_dict({**payload, "version": "20260903"})
```

- [ ] **Step 2: Run tests and observe missing package failure**

Run: `python -m pytest packages/strategy_manager/tests/test_models.py -q`

Expected: FAIL because `strategy_manager` does not exist.

- [ ] **Step 3: Implement minimal domain models**

Implement strict dataclasses with these public enums and identities:

```python
class Qualification(str, Enum):
    RESEARCH = "RESEARCH"
    PAPER_READY = "PAPER_READY"
    LIVE_READY = "LIVE_READY"
    RETIRED = "RETIRED"

STRATEGY_ID_PATTERN = re.compile(r"S[0-9]{3}$")
VERSION_PATTERN = re.compile(r"v[1-9][0-9]*$")
```

`canonical_sha256` must serialize with sorted keys, UTF-8, compact separators, and `ensure_ascii=False`. `release_payload()` excludes `release_hash` and lifecycle qualification.

- [ ] **Step 4: Verify package tests and static checks**

Run: `python -m pytest packages/strategy_manager/tests/test_models.py -q`

Run: `ruff check packages/strategy_manager`

Expected: all pass.

- [ ] **Step 5: Install the package editable and document installation**

Run: `python -m pip install -e ".\packages\strategy_manager[test]"`

Add the Strategy Manager install command before Trader/PTE installation in the root README.

- [ ] **Step 6: Commit**

```powershell
git add packages/strategy_manager README.md
git commit -m "feat: add strategy manager domain models"
```

### Task 2: Git registry, lifecycle, and evidence persistence

**Status:** Complete

**Files:**
- Create: `packages/strategy_manager/src/strategy_manager/registry.py`
- Create: `packages/strategy_manager/src/strategy_manager/lifecycle.py`
- Create: `packages/strategy_manager/tests/test_registry.py`
- Create: `packages/strategy_manager/tests/test_lifecycle.py`

**Interfaces:**
- Consumes: Task 1 models and validation.
- Produces: `StrategyRegistry(root: Path)`, `create_strategy`, `create_version`, `freeze_version`, `promote_version`, `downgrade_version`, `retire_version`, `record_evidence`, `current_qualification`, and `assert_deployable`.

- [ ] **Step 1: Write failing registry atomicity and lifecycle tests**

```python
def test_freeze_is_atomic_and_makes_version_immutable(tmp_path):
    registry = create_research_registry(tmp_path, strategy_id="S001", version="v1")
    evidence = research_evidence("S001", "v1", source_path="results.json")
    frozen = registry.freeze_version(
        "S001", "v1", actor="tester", reason="进入模拟盘", evidence=evidence
    )
    assert frozen.release_hash
    assert registry.current_qualification("S001", "v1") == Qualification.PAPER_READY
    version_path = tmp_path / "S001" / "versions" / "v1.json"
    tampered = json.loads(version_path.read_text(encoding="utf-8"))
    tampered["strategy_payload"]["changed"] = True
    version_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ImmutableVersionError):
        registry.validate_all()

def test_failed_promotion_writes_no_event(tmp_path):
    registry = create_paper_registry(tmp_path, strategy_id="S001", version="v1")
    before = registry.lifecycle_events("S001")
    with pytest.raises(EvidenceRequiredError):
        registry.promote_version(
            "S001", "v1", actor="tester", reason="证据不足", evidence_ids=[]
        )
    assert registry.lifecycle_events("S001") == before
```

Define `create_research_registry`, `create_paper_registry`, and `research_evidence` in
`packages/strategy_manager/tests/conftest.py`; each helper writes a complete valid object
using fixed `2026-09-03T10:00:00+08:00` timestamps so tests are deterministic.

- [ ] **Step 2: Run tests and observe missing registry behavior**

Run: `python -m pytest packages/strategy_manager/tests/test_registry.py packages/strategy_manager/tests/test_lifecycle.py -q`

Expected: FAIL because registry and transitions are not implemented.

- [ ] **Step 3: Implement file layout and atomic writes**

Use exactly:

```text
<root>/registry.json
<root>/S001/strategy.json
<root>/S001/versions/v1.json
<root>/S001/lifecycle.jsonl
<root>/S001/evidence.jsonl
<root>/S001/evidence/<evidence_id>.json
```

JSON writes use a sibling `.tmp` file followed by `Path.replace`. JSONL updates write the existing complete lines plus one new canonical line to a temporary file and replace atomically. Reject a write if the source file identity captured before validation changes before replacement.

- [ ] **Step 4: Implement allowed transitions**

```python
ALLOWED = {
    Qualification.RESEARCH: {Qualification.PAPER_READY, Qualification.RETIRED},
    Qualification.PAPER_READY: {Qualification.LIVE_READY, Qualification.RETIRED},
    Qualification.LIVE_READY: {Qualification.PAPER_READY, Qualification.RETIRED},
    Qualification.RETIRED: set(),
}
```

Freeze requires a self-contained research evidence input. Promotion requires at least one registered `PAPER_FORWARD` evidence ID. All lifecycle writes require nonblank actor and reason.

- [ ] **Step 5: Verify persistence tests**

Run: `python -m pytest packages/strategy_manager/tests -q`

Expected: all pass.

- [ ] **Step 6: Commit**

```powershell
git add packages/strategy_manager
git commit -m "feat: persist strategy lifecycle and evidence"
```

### Task 3: Register current strategy as S001-v1

**Status:** Complete

**Files:**
- Create: `configs/strategies/registry.json`
- Create: `configs/strategies/S001/strategy.json`
- Create: `configs/strategies/S001/versions/v1.json`
- Create: `configs/strategies/S001/lifecycle.jsonl`
- Create: `configs/strategies/S001/evidence.jsonl`
- Create: `tests/test_strategy_registry.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `StrategyRegistry` and current `baseline_20260903` plus EX20/EX01 artifacts.
- Produces: legacy alias `baseline_20260903 -> S001-v1`, qualification `PAPER_READY`, and a verified research evidence record.

- [ ] **Step 1: Write failing repository contract tests**

```python
def test_current_strategy_resolves_by_id_and_legacy_alias():
    registry = StrategyRegistry(REPO_ROOT / "configs" / "strategies")
    by_id = registry.get_version("S001", "v1")
    by_alias = registry.resolve_strategy("baseline_20260903")
    assert by_alias.release_id == by_id.release_id == "S001-v1"
    assert registry.current_qualification("S001", "v1") == Qualification.PAPER_READY
    assert by_id.strategy_payload["candidate_id"] == 143
```

- [ ] **Step 2: Run and observe missing registry data**

Run: `python -m pytest tests/test_strategy_registry.py -q`

Expected: FAIL because `configs/strategies` is absent.

- [ ] **Step 3: Generate S001-v1 deterministically**

Create `S001` named `综合基线策略`. Copy the complete signal/execution payload from `baseline_20260903` into `v1`; retain its original version and hash under `legacy_identity`. Record `0901_EX20` candidate 143 and `0903_EX01` execution review as sources. Compute `release_hash` using Task 1 canonical hashing.

- [ ] **Step 4: Record initial lifecycle and evidence**

Append `VERSION_CREATED` (`None -> RESEARCH`) and `VERSION_FROZEN` (`RESEARCH -> PAPER_READY`) events. Add one `RESEARCH_BACKTEST` evidence record referencing tracked EX20/EX01 machine artifacts and their hashes.

- [ ] **Step 5: Verify the registry and all immutable archives**

Run: `python -m pytest tests/test_strategy_registry.py tests/test_identity_and_archives.py -q`

Run: `python -m pytest tests -q -m archive`

Expected: all pass without modifying historical experiment manifests.

- [ ] **Step 6: Commit**

```powershell
git add configs/strategies tests/test_strategy_registry.py pyproject.toml
git commit -m "feat: register comprehensive baseline as S001-v1"
```

### Task 4: Trader strategy façade and management commands

**Status:** Complete

**Files:**
- Create: `src/czsc_trader/application/strategy_service.py`
- Create: `src/czsc_trader/cli/strategy_commands.py`
- Create: `tests/test_strategy_cli.py`
- Modify: `src/czsc_trader/cli/main.py`

**Interfaces:**
- Consumes: `StrategyRegistry`.
- Produces: `czsc-trader strategy list/show/history/create/version create/freeze/promote/downgrade/retire/evidence add/performance/validate`.
- Every command returns the existing one-line command envelope: `status`, `command`, `result` or `error`.

- [ ] **Step 1: Write failing CLI read tests**

```python
def test_strategy_show_uses_formal_identity(capsys):
    code = main(["strategy", "show", "--strategy", "S001", "--version", "v1",
                 "--repo-root", str(REPO_ROOT)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["result"]["name"] == "综合基线策略"
    assert payload["result"]["qualification"] == "PAPER_READY"
```

- [ ] **Step 2: Write failing mutation command tests**

Exercise a temporary registry and assert `--reason` and evidence requirements, legal promotion/demotion, and no partial event on failure.

- [ ] **Step 3: Run tests and observe unknown command failure**

Run: `python -m pytest tests/test_strategy_cli.py -q`

Expected: FAIL because the `strategy` resource is not registered.

- [ ] **Step 4: Implement thin application and parser adapters**

Keep all lifecycle rules inside Strategy Manager. Trader converts argparse inputs to domain calls and serializes results; it must not reproduce transition tables or hashing logic.

- [ ] **Step 5: Add read-only baseline compatibility**

`baseline list/show/validate` continues to resolve historical baseline files. For `baseline_20260903`, responses add `strategy_id=S001`, `strategy_version=v1`, and `release_hash`; no baseline mutation command is introduced.

- [ ] **Step 6: Verify CLI and existing Trader tests**

Run: `python -m pytest tests/test_strategy_cli.py tests/test_cli_e2e.py tests/test_repository_contract.py -q`

Expected: all pass with one JSON document per CLI call.

- [ ] **Step 7: Commit**

```powershell
git add src/czsc_trader tests
git commit -m "feat: expose strategy management through trader"
```

### Task 5: Publish advice.v4 strategy contract

**Status:** Complete

**Files:**
- Modify: `src/czsc_trader/application/advice_service.py`
- Modify: `src/czsc_trader/cli/main.py`
- Modify: `tests/test_execution_policy.py`
- Modify: `tests/test_cli_e2e.py`

**Interfaces:**
- Consumes: `StrategyRegistry.resolve_strategy(reference: str, version: str | None = None) -> StrategyVersion` and `StrategyRegistry.assert_deployable(strategy_id: str, version: str, environment: str) -> StrategyVersion` with `environment="PAPER"`.
- Produces: `advice.v4` with a `strategy` object and no production `baseline` identity.

- [ ] **Step 1: Write failing advice.v4 tests**

```python
def test_advice_v4_contains_formal_strategy_identity():
    result = run_advice(
        AdviceRequest(
            strategy="S001",
            strategy_version="v1",
            symbol="588080",
            as_of="2026-09-02",
            account_state_path=FIXTURES / "flat_account.json",
        )
    )
    registered = StrategyRegistry(REPO_ROOT / "configs" / "strategies").get_version(
        "S001", "v1"
    )
    assert result.result["contract_version"] == "advice.v4"
    assert result.result["strategy"] == {
        "strategy_id": "S001", "name": "综合基线策略", "version": "v1",
        "release_id": "S001-v1", "release_hash": registered.release_hash,
        "qualification": "PAPER_READY",
    }
    assert "baseline" not in result.result
```

Also assert retired/research versions cannot produce deployable advice and that `decision_id` does not change when only name or qualification display metadata changes.

- [ ] **Step 2: Run tests and observe advice.v3 mismatch**

Run: `python -m pytest tests/test_execution_policy.py tests/test_cli_e2e.py -q -k "advice"`

Expected: FAIL because production advice is still v3.

- [ ] **Step 3: Implement strategy resolution and v4 payload**

Add `--strategy` and `--strategy-version`; retain `--baseline baseline_20260903` as a compatibility input that resolves to `S001-v1`. Use only release identity, account state, data identity, cycle target, and orders in `decision_id`.

- [ ] **Step 4: Verify deterministic advice**

Run the same CLI request twice and assert byte-equivalent semantic JSON and identical `decision_id`.

- [ ] **Step 5: Run focused Trader tests**

Run: `python -m pytest tests/test_execution_policy.py tests/test_cli_e2e.py tests/test_strategy_registry.py -q`

Expected: all pass.

- [ ] **Step 6: Commit**

```powershell
git add src/czsc_trader tests
git commit -m "feat: publish formal strategy identity in advice v4"
```

### Task 6: Bind PTE virtual accounts to strategy versions

**Status:** Complete

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/contracts.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/advice_client.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/store.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/virtual_models.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/virtual_engine.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/cli.py`
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/dashboard.py`
- Modify: `packages/paper_trading_engine/tests/test_advice_client.py`
- Modify: `packages/paper_trading_engine/tests/test_virtual_accounts.py`
- Modify: `packages/paper_trading_engine/tests/test_cli.py`
- Modify: `packages/paper_trading_engine/tests/test_web.py`

**Interfaces:**
- Consumes: `advice.v4.strategy` only; PTE still has no Strategy Manager import.
- Produces: virtual-account columns `strategy_id`, `strategy_name_snapshot`, `strategy_version`, `release_hash`, and `qualification_snapshot`.

- [ ] **Step 1: Write failing v4 parser and migration tests**

Assert malformed strategy IDs/hashes are rejected, v3 is rejected for new decisions, and an existing pristine or active `baseline-143` row gains `S001/v1` identity without changing cash, holdings, orders, fills, or timestamps that describe trading activity.

- [ ] **Step 2: Write failing page behavior tests**

Assert the main card contains `综合基线策略 · v1`, does not expose `baseline-143`, `baseline_20260903`, or candidate 143, and keeps internal IDs in structured diagnostics.

- [ ] **Step 3: Run focused tests and observe failures**

Run: `python -m pytest packages/paper_trading_engine/tests/test_advice_client.py packages/paper_trading_engine/tests/test_virtual_accounts.py packages/paper_trading_engine/tests/test_web.py -q`

Expected: FAIL on v4 parsing, missing account fields, and old labels.

- [ ] **Step 4: Implement additive SQLite migration**

Call the existing `_ensure_column` helper for `virtual_accounts` with these exact additions:

```python
self._ensure_column("virtual_accounts", "strategy_id", "TEXT")
self._ensure_column("virtual_accounts", "strategy_name_snapshot", "TEXT")
self._ensure_column("virtual_accounts", "strategy_version", "TEXT")
self._ensure_column("virtual_accounts", "release_hash", "TEXT")
self._ensure_column("virtual_accounts", "qualification_snapshot", "TEXT")
```

Migrate rows identified by the exact legacy baseline version/hash to `S001/v1`; abort startup if the row contains an unknown identity instead of guessing.

- [ ] **Step 5: Update advice client and virtual engine**

CLI calls use `--strategy S001 --strategy-version v1`. Decision parsing requires exact account-bound strategy identity. Qualification mismatch records an account-level error and does not settle or generate orders.

- [ ] **Step 6: Update CLI and dashboard terminology**

Account creation accepts `--strategy` and `--strategy-version`. Keep `--baseline` for one compatibility period. Main UI displays formal name/version and runtime status separately; account IDs, aliases, hashes, and research source remain in diagnostics.

- [ ] **Step 7: Verify all PTE tests**

Run: `python -m pytest packages/paper_trading_engine/tests -q`

Expected: all pass.

- [ ] **Step 8: Commit**

```powershell
git add packages/paper_trading_engine
git commit -m "feat: bind PTE accounts to strategy releases"
```

### Task 7: Cross-stage performance evidence

**Files:**
- Modify: `packages/paper_trading_engine/src/paper_trading_engine/cli.py`
- Create: `packages/paper_trading_engine/src/paper_trading_engine/performance_export.py`
- Create: `packages/paper_trading_engine/tests/test_performance_export.py`
- Modify: `src/czsc_trader/application/strategy_service.py`
- Modify: `tests/test_strategy_cli.py`

**Interfaces:**
- Produces: `pte performance export --account-id ID --output FILE` using the Strategy Manager evidence JSON schema without importing the package.
- Consumes: Trader `strategy evidence add --input FILE` and `strategy performance`.

- [ ] **Step 1: Write failing PTE export test**

Create a temporary virtual ledger with known snapshots and one closed trade. Assert exported values include period, 100,000 initial capital, fee rate, maximum drawdown, Calmar, win/loss status, return, closed trades, release identity, and a source hash.

- [ ] **Step 2: Write failing Trader evidence import test**

Import the PTE file into a temporary strategy registry. Assert the source file is copied to `S001/evidence/<evidence_id>.json`, its hash is verified, and `strategy performance` groups evidence by `RESEARCH_BACKTEST`, `PAPER_FORWARD`, and `LIVE` without joining their equity curves.

- [ ] **Step 3: Run tests and observe unknown commands**

Run: `python -m pytest packages/paper_trading_engine/tests/test_performance_export.py tests/test_strategy_cli.py -q`

Expected: FAIL because export and import are absent.

- [ ] **Step 4: Implement pure PTE exporter**

Derive metrics from the selected account's persisted snapshots and fills. Emit one UTF-8 JSON object through an atomic output write; do not mutate the ledger or Strategy Manager registry.

- [ ] **Step 5: Implement Trader import and grouped display**

Validate with Strategy Manager, copy the evidence artifact, append the index record, and return a stable JSON result. Reject duplicate IDs with different hashes; identical repeats are idempotent.

- [ ] **Step 6: Verify performance tests**

Run: `python -m pytest packages/paper_trading_engine/tests/test_performance_export.py tests/test_strategy_cli.py -q`

Expected: all pass.

- [ ] **Step 7: Commit**

```powershell
git add packages/paper_trading_engine src/czsc_trader tests
git commit -m "feat: exchange strategy performance evidence"
```

### Task 8: Documentation, runtime cutover, and complete verification

**Files:**
- Modify: `README.md`
- Modify: `docs/DEVELOPMENT_HANDOFF.md`
- Modify: `docs/RESEARCH_HANDOFF.md`
- Modify: `packages/paper_trading_engine/README.md`
- Modify: `tests/test_repository_contract.py`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: cross-machine operating instructions and final repository/runtime verification evidence.

- [ ] **Step 1: Update user and handoff documentation**

Document `S001 / 综合基线策略 / v1`, qualification semantics, Trader commands, PTE account creation, performance evidence flow, historical aliases, and the rule that runtime state remains outside SM.

- [ ] **Step 2: Add final architecture contract tests**

Assert Strategy Manager has no forbidden imports, PTE has no `strategy_manager` import, production advice is v4, the active strategy registry is the production source, and all historical experiment archives remain unchanged.

- [ ] **Step 3: Run the complete automated verification**

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m pytest packages\strategy_manager\tests -q
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests -q
.\.venv\Scripts\python.exe -m pytest tests -q -m archive
.\.venv\Scripts\ruff.exe check src tests packages\strategy_manager packages\paper_trading_engine\src packages\paper_trading_engine\tests
.\.venv\Scripts\python.exe -m compileall -q src packages\strategy_manager\src packages\paper_trading_engine\src
git diff --check
```

Expected: every command exits zero.

- [ ] **Step 4: Perform isolated runtime smoke test**

Start PTE on port 18080 with a new temporary database. Verify `/api/status` reports `advice.v4`, `S001`, `综合基线策略`, `v1`, `PAPER_READY`, 100,000 virtual cash, no duplicate order/fill after two refreshes, and no raw internal IDs on the main page.

- [ ] **Step 5: Cut over the formal local runtime**

Allow the watchdog to relaunch the PTE child or restart it through the existing service command when administrator access is available. Verify port 8080 returns the same strategy identity, keeps existing ledger balances and holdings, and reports zero current scheduler failures.

- [ ] **Step 6: Commit**

```powershell
git add README.md docs packages/paper_trading_engine/README.md tests/test_repository_contract.py
git commit -m "docs: hand off strategy manager operations"
```

- [ ] **Step 7: Report branch state**

Report commits, tests, runtime status, compatibility behavior, and any machine-level action still requiring administrator rights. Keep `codex/strategy-manager` unmerged and unpushed until explicitly authorized.
