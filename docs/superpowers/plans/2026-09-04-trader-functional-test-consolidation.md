# Trader Functional Test Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Trader's 106 fine-grained test functions with eight deterministic, user-facing functional scenarios.

**Architecture:** Tests enter through the installed CLI or Trader application boundary and verify observable artifacts, decisions, audit records, and errors. A temporary repository plus fixed in-memory market data isolates the suite from the active strategy registry, live vendors, PTE runtime, and historical full-scale searches.

**Tech Stack:** Python 3.12, pytest, pandas, pathlib, subprocess, existing Trader CLI/application services, SM and SE package APIs.

**Spec:** `docs/superpowers/specs/2026-09-04-trader-functional-test-consolidation-design.md`

## Global Constraints

- Keep exactly eight default Trader functional scenarios named `test_ft_t01_*` through `test_ft_t08_*`.
- Do not change production behavior, S001 strategy data, active versions, execution rules, or historical experiment archives.
- Do not access live Tushare, Futu OpenD, the running PTE database, or Windows service control.
- Use a temporary repository and deterministic fixed data for all mutable workflows.
- Verify safety rules through observable decisions, orders, artifacts, or errors.
- Delete migrated fine-grained tests after their replacement scenario passes.
- Keep formal historical archive validation as an explicit operational command outside default pytest.
- Target a cold default Trader test duration of at most 30 seconds and preferably 15 to 20 seconds.
- Execute this plan inline in the primary session because repository instructions default to no subagents and no worktrees.

---

## File Structure

**Create**

- `tests/functional/conftest.py`: shared temporary repository, market frame, JSON CLI, and evaluation-bundle helpers.
- `tests/functional/test_cli_surface.py`: FT-T08 installed entry point and command surface.
- `tests/functional/test_data_and_advice.py`: FT-T01 data publication and FT-T02 advice lifecycle.
- `tests/functional/test_backtest.py`: FT-T03 audited backtest.
- `tests/functional/test_charting.py`: FT-T04 chart hover contract.
- `tests/functional/test_strategy_lifecycle.py`: FT-T05 Strategy CLI lifecycle.
- `tests/functional/test_evaluation.py`: FT-T06 candidate evaluation and acceptance.
- `tests/functional/test_archive.py`: FT-T07 portable archive validation.

**Modify**

- `pyproject.toml`: make `tests/functional` the Trader default test path and remove the archive marker configuration.
- `README.md`: replace old Trader test commands with the functional command and explicit archive operation.
- `docs/DEVELOPMENT_HANDOFF.md`: document the new regression boundary and command.
- `docs/RESEARCH_HANDOFF.md`: state that historical archive validation is an operation, not default pytest.

**Delete after replacement tests pass**

- Every existing `tests/test_*.py` file listed in the approved spec.

---

### Task 1: Shared Isolation Fixtures and Installed CLI Surface

**Files:**

- Create: `tests/functional/conftest.py`
- Create: `tests/functional/test_cli_surface.py`

**Interfaces:**

- Produces: `functional_repo(tmp_path: Path) -> Path`, `vendor_frame(rows) -> DataFrame`, `invoke_main(args, capsys) -> dict`, `write_evaluation_bundle(root: Path) -> Path`.
- Consumes: `czsc_trader.cli.main.build_parser`, installed `czsc-trader` executable.

- [ ] **Step 1: Create isolated repository helpers**

Implement `functional_repo` so it creates a minimal `pyproject.toml`, `src/czsc_trader`, `configs`, `data/raw`, `experiments`, and `outputs`; copy only `configs/strategies` and the strategy-linked baseline files needed by the scenario. Implement `invoke_main` as:

```python
def invoke_main(arguments: list[str], capsys) -> dict:
    from czsc_trader.cli.main import main

    exit_code = main(arguments)
    output = capsys.readouterr()
    assert output.err == ""
    payload = json.loads(output.out)
    assert exit_code == 0
    assert payload["status"] == "PASS"
    return payload
```

- [ ] **Step 2: Add FT-T08**

Start the installed executable once with `--help`, then inspect `build_parser()` in-process. Assert the exact top-level resources and required action sets:

```python
EXPECTED_ACTIONS = {
    "data": {"prepare", "validate"},
    "baseline": {"list", "show", "validate"},
    "strategy": {
        "list", "validate", "show", "history", "performance", "create",
        "version", "freeze", "promote", "downgrade", "retire", "evidence",
        "evaluate", "accept-evaluation",
    },
    "backtest": {"run"},
    "advice": {"run"},
    "archive": {"validate"},
}

def test_ft_t08_installed_cli_exposes_supported_command_surface():
    completed = subprocess.run(
        [str(CLI), "--help"], capture_output=True, text=True, encoding="utf-8"
    )
    assert completed.returncode == 0
    assert set(EXPECTED_ACTIONS) <= set(completed.stdout.split())
    assert command_surface(build_parser()) == EXPECTED_ACTIONS
```

`command_surface` must inspect argparse subparser choices without starting a second process.

- [ ] **Step 3: Run the new scenario**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\functional\test_cli_surface.py -q
```

Expected: one test passes and only one external CLI process is started.

- [ ] **Step 4: Commit the fixture and CLI surface**

```powershell
git add tests/functional/conftest.py tests/functional/test_cli_surface.py
git commit -m "test: add isolated trader functional harness"
```

---

### Task 2: Data Publication and Advice Lifecycle

**Files:**

- Create: `tests/functional/test_data_and_advice.py`
- Modify: `tests/functional/conftest.py`

**Interfaces:**

- Consumes: `functional_repo`, `vendor_frame`, `invoke_main` from Task 1.
- Produces: FT-T01 and FT-T02; reusable fixed daily, weekly, 30-minute, execution-price, and calendar fixtures.

- [ ] **Step 1: Add FT-T01 with simulated vendor functions**

Patch the data preparation boundary so `data prepare` receives fixed frames and the next SSE session `2026-09-02`. Execute `data validate`, load the published execution price, then append a byte to the execution CSV and assert validation fails.

Key assertions:

```python
def test_ft_t01_data_prepare_validate_and_tamper_detection(
    functional_repo: Path, capsys, monkeypatch
):
    data_dir = functional_repo / "data" / "raw"
    prepared = invoke_main([
        "data", "prepare", "--symbol", "588080.SH", "--asset", "etf",
        "--start", "2026-09-01", "--end", "2026-09-01",
        "--data-dir", str(data_dir), "--repo-root", str(functional_repo),
    ], capsys)
    assert prepared["result"]["next_trading_session"] == "2026-09-02"
    validated = invoke_main([
        "data", "validate", "--symbol", "588080.SH",
        "--repo-root", str(functional_repo),
    ], capsys)
    assert validated["status"] == "PASS"
    prices = load_execution_prices(data_dir, "588080.SH", "etf")
    assert prices.iloc[-1]["close"] == pytest.approx(1.688)
    execution_csv.write_bytes(execution_csv.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="SHA-256 differs"):
        load_execution_prices(data_dir, "588080.SH", "etf")
```

- [ ] **Step 2: Add FT-T02 as one complete account path**

Build a fixed strategy and market sequence that enters, retries while unfilled, holds after a fill, and exits. Invoke `advice run` for each state and retain every response in one `steps` list.

Key assertions:

```python
def test_ft_t02_advice_covers_entry_retry_hold_and_exit(
    functional_repo: Path, capsys, monkeypatch
):
    entry, retry, holding, exit_decision = steps
    assert all(row["contract_version"] == "advice.v4" for row in steps)
    assert entry["order"]["quantity"] % 100 == 0
    assert entry["order"]["limit_price"] * 1000 % 1 == pytest.approx(0)
    assert entry["effective_session"] == "2026-09-02"
    assert retry["target_quantity"] == entry["target_quantity"]
    assert holding["delta_quantity"] == 0
    assert holding["order"] is None
    assert exit_decision["order"]["side"] == "SELL"
    assert exit_decision["order"]["quantity"] == holding["actual_quantity"]
```

Within the same scenario, invoke one invalid account state and assert the CLI returns an error containing the account quantity reason. This keeps validation observable without creating a ninth test.

- [ ] **Step 3: Run both scenarios**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\functional\test_data_and_advice.py -q
```

Expected: exactly two tests pass without network access.

- [ ] **Step 4: Commit data and advice coverage**

```powershell
git add tests/functional/conftest.py tests/functional/test_data_and_advice.py
git commit -m "test: cover trader data and advice workflows"
```

---

### Task 3: Audited Backtest and Chart Interaction

**Files:**

- Create: `tests/functional/test_backtest.py`
- Create: `tests/functional/test_charting.py`

**Interfaces:**

- Consumes: fixed repository/data helpers from Tasks 1 and 2.
- Produces: FT-T03 and FT-T04.

- [ ] **Step 1: Add FT-T03**

Run `backtest run` against a compact deterministic window. Assert the business metric schema, next-session orders, legal ETF quantities/prices, and required artifacts.

```python
def test_ft_t03_backtest_publishes_audited_metrics_orders_and_reports(
    functional_repo: Path, capsys
):
    payload = invoke_main([
        "backtest", "run", "--symbol", "588080.SH", "--asset", "etf",
        "--start", "2026-01-01", "--end", "2026-02-28",
        "--outputs-root", str(functional_repo / "outputs"),
        "--repo-root", str(functional_repo),
    ], capsys)
    output_dir = Path(payload["artifacts"]["output_dir"])
    required = {
        "manifest.json", "audit.json", "report.md", "chart.html",
        "execution_orders.csv", "execution_equity.csv",
    }
    assert required <= {path.name for path in output_dir.iterdir()}
    metrics = payload["result"]["windows"]["full"]["strategies"]
    assert set(metrics["active_baseline_execution"]) == {
        "max_drawdown", "calmar", "win_loss_ratio", "win_loss_ratio_status",
        "return", "sharpe",
    }
    orders = pd.read_csv(output_dir / "execution_orders.csv")
    assert (orders["size"] % 100 == 0).all()
    assert next_session_relationship_is_valid(orders, sessions)
```

Include exact expected values for one deterministic net return, maximum drawdown, and fee deduction so the scenario detects numerical drift.

- [ ] **Step 2: Add FT-T04**

Build the four-session chart fixture from the existing chart regression and write it to HTML. Assert unified hover mode, one transparent daily hover target for every session, candlestick hover suppression, range breaks, and buy/sell markers.

```python
def test_ft_t04_chart_keeps_every_session_targetable_from_price_panel(
    tmp_path: Path, monkeypatch
):
    figure = build_period_chart(
        daily, factors, pd.DataFrame(), dates[0], dates[-1], "hover contract"
    )
    hover_target = next(trace for trace in figure.data if trace.name == "日K数据")
    assert figure.layout.hovermode == "x unified"
    assert figure.layout.hoversubplots == "axis"
    assert list(pd.to_datetime(hover_target.x)) == list(dates)
    assert hover_target.marker.color == "rgba(0,0,0,0)"
    assert next(trace for trace in figure.data if trace.type == "candlestick").hoverinfo == "skip"
    figure.write_html(output_path)
    assert output_path.is_file()
```

- [ ] **Step 3: Run backtest and chart scenarios**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\functional\test_backtest.py tests\functional\test_charting.py -q
```

Expected: exactly two tests pass.

- [ ] **Step 4: Commit backtest and chart coverage**

```powershell
git add tests/functional/test_backtest.py tests/functional/test_charting.py
git commit -m "test: cover trader backtest and chart workflows"
```

---

### Task 4: Strategy CLI Lifecycle

**Files:**

- Create: `tests/functional/test_strategy_lifecycle.py`
- Modify: `tests/functional/conftest.py`

**Interfaces:**

- Consumes: `functional_repo` and `invoke_main`.
- Produces: FT-T05 with a complete mutable strategy lifecycle and legacy baseline compatibility.

- [ ] **Step 1: Add the complete lifecycle scenario**

Use only the temporary repository. Execute every Strategy CLI mutation and read command in lifecycle order. Add a valid `RESEARCH_BACKTEST` evidence bundle before promotion and verify performance grouping.

```python
def test_ft_t05_strategy_cli_manages_a_complete_audited_lifecycle(
    functional_repo: Path, capsys
):
    common = ["--actor", "tester", "--reason", "functional test"]
    root = ["--repo-root", str(functional_repo)]
    created = invoke_main(
        ["strategy", "create", "--input", str(strategy_input), *common, *root], capsys
    )
    versioned = invoke_main([
        "strategy", "version", "create", "--input", str(version_input),
        *common, *root,
    ], capsys)
    frozen = invoke_main([
        "strategy", "freeze", "--strategy", "S900", "--version", "v1",
        "--evidence", str(freeze_evidence), *common, *root,
    ], capsys)
    shown = invoke_main([
        "strategy", "show", "--strategy", "S900", "--version", "v1", *root,
    ], capsys)
    assert shown["result"]["release_hash"] == frozen["result"]["release_hash"]
    invoke_main([
        "strategy", "evidence", "add", "--input", str(performance_evidence), *root,
    ], capsys)
    performance = invoke_main([
        "strategy", "performance", "--strategy", "S900", "--version", "v1", *root,
    ], capsys)
    assert len(performance["result"]["phases"]["RESEARCH_BACKTEST"]) == 1
    invoke_main([
        "strategy", "promote", "--strategy", "S900", "--version", "v1",
        "--evidence", "EVD-S900-PAPER", *common, *root,
    ], capsys)
    invoke_main([
        "strategy", "downgrade", "--strategy", "S900", "--version", "v1",
        "--evidence", "EVD-S900-LIVE", *common, *root,
    ], capsys)
    retired = invoke_main([
        "strategy", "retire", "--strategy", "S900", "--version", "v1",
        *common, *root,
    ], capsys)
    assert retired["result"]["to_state"] == "RETIRED"
```

After lifecycle mutations, call `list`, `validate`, `history`, `performance`, `baseline list`, `baseline show`, and `baseline validate`. Derive expected event and version counts from mutations performed in the scenario; do not use constants tied to the production registry.

- [ ] **Step 2: Run the lifecycle scenario**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\functional\test_strategy_lifecycle.py -q
```

Expected: one test passes and no tracked strategy file changes.

- [ ] **Step 3: Commit lifecycle coverage**

```powershell
git add tests/functional/conftest.py tests/functional/test_strategy_lifecycle.py
git commit -m "test: cover trader strategy lifecycle"
```

---

### Task 5: Candidate Evaluation, Acceptance, and Archive Validation

**Files:**

- Create: `tests/functional/test_evaluation.py`
- Create: `tests/functional/test_archive.py`
- Modify: `tests/functional/conftest.py`

**Interfaces:**

- Consumes: isolated repository helpers and `write_evaluation_bundle`.
- Produces: FT-T06 and FT-T07.

- [ ] **Step 1: Implement the deterministic evaluation fixture**

Create an incumbent, a winner, a behavior duplicate, and an inferior candidate. The local runner returns fixed `MetricObservation` rows for SCREENING, FORMAL, and STRESS tiers. Use OPC-v3 and a fixed statistical-audit response with `PASS` plus `MIXED` risk so persistence of the SE result is observable.

- [ ] **Step 2: Add FT-T06**

Run `strategy evaluate`, inspect the complete funnel and audit artifacts, rerun to prove cache reuse, tamper with one reuse artifact and assert rejection, restore it, then run `strategy accept-evaluation` three times with a PTE runner that fails once and succeeds once.

```python
def test_ft_t06_evaluation_audits_every_candidate_and_freezes_once(
    functional_repo: Path, capsys, monkeypatch
):
    evaluated = invoke_main([
        "strategy", "evaluate", "--experiment", "0904_TEST",
        "--repo-root", str(functional_repo),
    ], capsys)
    assert evaluated["result"]["decision"] == "RECOMMEND_FREEZE"
    assert evaluated["result"]["audit"]["risk_label"] == "MIXED"
    decisions = read_csv_rows(artifacts / "screening_decisions.csv")
    assert {row["candidate_id"] for row in decisions} == {
        "winner", "duplicate", "inferior",
    }
    assert outcome(decisions, "winner") == "SHORTLISTED"
    assert outcome(decisions, "duplicate") == "BEHAVIOR_DEDUPLICATED"
    assert outcome(decisions, "inferior") == "SCREENING_NONINFERIORITY"
    context = RepositoryContext.discover(functional_repo, explicit_root=functional_repo)
    first = accept_evaluation(
        context, "0904_TEST", "tester", "functional test", pte_runner=fail_then_succeed
    )
    second = accept_evaluation(
        context, "0904_TEST", "tester", "functional test", pte_runner=fail_then_succeed
    )
    third = accept_evaluation(
        context, "0904_TEST", "tester", "functional test", pte_runner=fail_then_succeed
    )
    assert first.result["activation_state"] == "PAPER_ACTIVATION_PENDING"
    assert second.result["activation_state"] == "PAPER_ACTIVE"
    assert third.result == second.result
    assert exactly_one_new_strategy_version(repo)
```

Keep all checks inside this single workflow test. Do not create separate tests for each malformed protocol field.

- [ ] **Step 3: Add FT-T07**

Build one temporary archive with the four required research documents and manifest. Validate it, confirm runtime cache exclusion and append-only acceptance journal behavior, then tamper with a declared file and assert validation failure.

```python
def test_ft_t07_archive_validation_is_portable_and_detects_tampering(
    functional_repo: Path, capsys
):
    build_experiment_manifest(archive, metadata)
    command = [
        "archive", "validate", "--archive", str(archive),
        "--repo-root", str(functional_repo),
    ]
    first = invoke_main(command, capsys)
    assert first["status"] == "PASS"
    (archive / "__pycache__" / "ignored.pyc").write_bytes(b"runtime")
    append_acceptance_journal(archive, acceptance)
    second = invoke_main(command, capsys)
    assert second["status"] == "PASS"
    (archive / "04_conclusion.md").write_text("tampered", encoding="utf-8")
    assert_cli_failure(command, capsys)
```

- [ ] **Step 4: Run evaluation and archive scenarios**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\functional\test_evaluation.py tests\functional\test_archive.py -q
```

Expected: exactly two tests pass without full-scale candidate evaluation.

- [ ] **Step 5: Commit evaluation and archive coverage**

```powershell
git add tests/functional/conftest.py tests/functional/test_evaluation.py tests/functional/test_archive.py
git commit -m "test: cover trader evaluation and archive workflows"
```

---

### Task 6: Remove Fine-Grained Tests and Switch the Default Gate

**Files:**

- Delete: all 24 existing `tests/test_*.py` files from the approved design mapping.
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `docs/DEVELOPMENT_HANDOFF.md`
- Modify: `docs/RESEARCH_HANDOFF.md`

**Interfaces:**

- Consumes: all eight passing functional scenarios.
- Produces: one default Trader functional regression command and one explicit historical archive operation.

- [ ] **Step 1: Prove all eight replacement scenarios pass before deletion**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\functional -q --durations=8
```

Expected: `8 passed`, no deselection, no external-network access, duration at most 30 seconds.

- [ ] **Step 2: Delete the migrated test files**

Delete all root-level `tests/test_*.py` files listed in section 4 of the approved spec. Preserve `tests/functional/` only.

- [ ] **Step 3: Configure the default Trader test path**

Change pytest configuration to:

```toml
[tool.pytest.ini_options]
testpaths = ["tests/functional"]
addopts = "--tb=short"
```

Remove the `archive` marker because historical archive validation is no longer a pytest tier.

- [ ] **Step 4: Update operator documentation**

Document the default command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Document formal archive verification as the product operation:

```powershell
.\.venv\Scripts\czsc-trader.exe archive validate --all --repo-root .
```

State that temporary TDD tests are removed once their behavior is represented in one of FT-T01 through FT-T08.

- [ ] **Step 5: Verify collection, behavior, duration, and unchanged product state**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest --collect-only -q
.\.venv\Scripts\python.exe -m pytest -q --durations=8
.\.venv\Scripts\python.exe -m pytest packages\strategy_manager\tests -q
.\.venv\Scripts\python.exe -m pytest packages\strategy_evaluator\tests -q
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests -q
node --test packages\paper_trading_engine\tests\js\console_state.test.mjs
.\.venv\Scripts\python.exe -m compileall -q src
.\.venv\Scripts\python.exe -m pip check
git diff --check
git status --short
```

Expected:

- Trader collection reports exactly 8 tests.
- All eight Trader tests pass within 30 seconds.
- SM, SE, PTE Python, and PTE JavaScript suites still pass.
- Compile, dependency, and diff checks exit zero.
- No files under `configs/strategies`, `configs/rule_baselines`, `experiments`, or `state` are modified.

- [ ] **Step 6: Commit the completed consolidation**

```powershell
git add pyproject.toml README.md docs/DEVELOPMENT_HANDOFF.md docs/RESEARCH_HANDOFF.md tests
git commit -m "test: consolidate trader regression into functional workflows"
```

---

## Self-Review Record

- Spec coverage: all eight approved scenarios map to one named test and one implementation task.
- Deletion safety: every old test file is deleted only after its replacement scenario passes.
- External boundaries: live vendors, PTE runtime, Windows services, and production strategy state are excluded.
- Type consistency: shared fixture names are defined in Task 1 and consumed unchanged later.
- Placeholder scan: the plan contains no deferred implementation markers.
- Runtime control: one installed subprocess, small candidate pool, fixed data, and no historical full-scale evaluation.
