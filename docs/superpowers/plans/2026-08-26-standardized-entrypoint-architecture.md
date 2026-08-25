# Standardized Entrypoint Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Replace every user-facing Python script and runner CLI with one deterministic czsc-trader command while preserving baseline results and Git experiment archives.

**Architecture:** A thin cli package converts arguments into application requests and serializes one result envelope. Application services own repository discovery, safety checks, execution, and publication; an explicit research handler registry replaces experiment-type conditionals while existing numerical engines remain focused domain dependencies.

**Tech Stack:** Python 3.12, argparse, dataclasses, pathlib, CZSC 1.0.1, vectorbt 1.1.0, pandas 3.x, pytest.

**Spec:** docs/superpowers/specs/2026-08-26-standardized-entrypoint-architecture-design.md

## Global Constraints

- Work on the current master; do not create a Git worktree or use subagents.
- Do not modify tracked files under experiments or frozen baseline JSON.
- baseline_20260826 remains active only for 588080.SH; baseline_20260823 remains archived and explicit-only.
- Default stdout is exactly one JSON document; progress and diagnostics use stderr.
- Frozen experiments cannot run in place; replay output must be outside the source archive.
- Do not retain compatibility scripts, deprecated wrappers, alias modules, or duplicate CLIs.
- Preserve data boundaries, next-open execution, fees, audit semantics, and recorded research conclusions.
- Each task follows red-green-refactor and ends in a focused commit.

---

### Task 1: CLI contracts, repository context, and sole console entry

**Files:**
- Create: src/czsc_trader/application/{__init__,context,errors,results}.py
- Create: src/czsc_trader/cli/{__init__,main,output}.py
- Modify: pyproject.toml
- Test: tests/test_application_context.py
- Test: tests/test_cli_contract.py

**Interfaces:**
- Produces RepositoryContext.discover(start, explicit_root=None).
- Produces immutable CommandResult and typed CommandError hierarchy.
- Produces cli.main.main(argv=None) returning an integer exit code.

- [ ] **Step 1: Write failing context and envelope tests**

~~~python
def test_context_discovers_repo_from_nested_directory(tmp_path):
    root = tmp_path / "repo"
    (root / "src" / "czsc_trader").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    assert RepositoryContext.discover(nested).root == root.resolve()

def test_cli_stdout_is_one_json_document(capsys):
    code = main(["baseline", "list", "--repo-root", "."])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["command"] == "baseline.list"
~~~

- [ ] **Step 2: Run focused tests and confirm collection fails because new modules do not exist**

Run: .\.venv\Scripts\python.exe -m pytest tests\test_application_context.py tests\test_cli_contract.py -q

- [ ] **Step 3: Implement RepositoryContext, CommandResult, and CommandError**

RepositoryContext resolves root, raw_dir, baseline_root, experiments_root, and outputs_root. Discovery walks parents for pyproject.toml plus src/czsc_trader. Errors map usage/protocol, validation, safety, execution, and internal failures to exits 2, 3, 4, 5, and 10.

- [ ] **Step 4: Implement parser skeleton and serializer**

Create data, baseline, backtest, experiment, and archive subparsers. Dispatch through args.command_handler. Serialize one UTF-8 JSON object to stdout; debug tracebacks go only to stderr.

- [ ] **Step 5: Register the only console script**

~~~toml
[project.scripts]
czsc-trader = "czsc_trader.cli.main:main"
~~~

- [ ] **Step 6: Run focused tests and commit**

Run: .\.venv\Scripts\python.exe -m pytest tests\test_application_context.py tests\test_cli_contract.py -q

Commit: feat: establish unified command contracts

### Task 2: Data and baseline application services

**Files:**
- Create: src/czsc_trader/application/data_service.py
- Create: src/czsc_trader/application/baseline_service.py
- Modify: src/czsc_trader/cli/main.py
- Test: tests/test_data_service.py
- Test: tests/test_baseline_service.py
- Modify: tests/test_cli_contract.py

**Interfaces:**
- Produces prepare_data, validate_data, list_baselines, show_baseline, and validate_baseline.
- Consumes existing market_data_prep and baselines domain functions without duplicating validation.

- [ ] **Step 1: Write failing service tests**

~~~python
def test_list_baselines_marks_active_and_archived(repo_context):
    rows = list_baselines(repo_context).result["baselines"]
    statuses = {row["version"]: row["status"] for row in rows}
    assert statuses["baseline_20260826"] == "active"
    assert statuses["baseline_20260823"] == "archived"

def test_validate_data_reports_cutoff(repo_context):
    result = validate_data(repo_context, "588080.SH")
    assert result.status == "PASS"
    assert result.result["requested_end"] == "2026-08-24"
~~~

- [ ] **Step 2: Run focused tests and confirm RED**

Run: .\.venv\Scripts\python.exe -m pytest tests\test_data_service.py tests\test_baseline_service.py -q

- [ ] **Step 3: Implement thin services and CLI wiring**

data prepare calls prepare_market_data. data validate checks manifest/validation identity. Baseline commands call resolve_baseline and expose registry metadata without reimplementing hashes.

- [ ] **Step 4: Verify commands**

Run .\.venv\Scripts\czsc-trader.exe baseline list --repo-root .
Run .\.venv\Scripts\czsc-trader.exe data validate --symbol 588080.SH --repo-root .

Expected: exit 0 and one JSON document per command.

- [ ] **Step 5: Run focused tests and commit**

Commit: feat: unify data and baseline commands

### Task 3: Backtest service and atomic publication

**Files:**
- Create: src/czsc_trader/application/backtest_service.py
- Create: src/czsc_trader/reporting/{__init__,publication}.py
- Modify: src/czsc_trader/backtest_runner.py
- Modify: src/czsc_trader/cli/main.py
- Test: tests/test_backtest_service.py
- Modify: tests/test_backtest_runner.py

**Interfaces:**
- Produces immutable BacktestCommand.
- Produces run_backtest(context, request) returning CommandResult.
- Produces publish_directory(staging, destination) using same-filesystem atomic rename.

- [ ] **Step 1: Write the failing 588080 parity test**

~~~python
def test_service_preserves_active_baseline_result(repo_context, tmp_path):
    result = run_backtest(repo_context, BacktestCommand(
        symbol="588080.SH", asset_type="etf",
        start=date(2026, 1, 1), end=date(2026, 8, 21),
        outputs_root=tmp_path,
    ))
    metrics = result.result["windows"]["full"]
    assert metrics["strategy_return"] == pytest.approx(0.6197253904580182)
    assert metrics["trade_count"] == 10
~~~

- [ ] **Step 2: Write failure-atomicity tests**

Patch audit to fail; assert no final output exists and staging is removed.

- [ ] **Step 3: Run focused tests and confirm RED**

Run: .\.venv\Scripts\python.exe -m pytest tests\test_backtest_service.py -q

- [ ] **Step 4: Implement service and publication boundary**

Keep numerical work in run_fixed_backtest. Move request conversion and final publication to the application layer. Preserve manifest, metrics, orders, events, factors, audit, report, and chart formats.

- [ ] **Step 5: Run CLI parity smoke**

Run: .\.venv\Scripts\czsc-trader.exe backtest run --symbol 588080.SH --asset etf --start 2026-01-01 --end 2026-08-21 --repo-root .

Expected: baseline_20260826, return 0.6197253904580182, 10 trades, audit PASS.

- [ ] **Step 6: Run focused regression and commit**

Run: .\.venv\Scripts\python.exe -m pytest tests\test_backtest_service.py tests\test_backtest_runner.py tests\test_baseline_execution.py tests\test_baselines.py -q

Commit: feat: route backtests through application service

### Task 4: Research handler contract, registry, and archive service

**Files:**
- Create: src/czsc_trader/research/{__init__,contracts,registry}.py
- Create: src/czsc_trader/application/{archive_service,experiment_service}.py
- Test: tests/test_research_registry.py
- Test: tests/test_experiment_service.py
- Test: tests/test_archive_service.py

**Interfaces:**
- Produces ExperimentHandler with handler_id, validate_protocol, and run.
- Produces ExperimentRegistry.register, resolve_protocol, and validate.
- Produces run_experiment, replay_experiment, and validate_archives.

- [ ] **Step 1: Write failing registry completeness tests**

~~~python
def test_all_tracked_protocols_resolve(repo_context):
    registry = build_default_registry()
    for path in sorted(repo_context.experiments_root.iterdir()):
        assert registry.resolve_protocol(load_protocol(path), path.name)

def test_duplicate_handler_is_rejected():
    registry = ExperimentRegistry()
    registry.register(FakeHandler("same"))
    with pytest.raises(ProtocolError, match="duplicate"):
        registry.register(FakeHandler("same"))
~~~

- [ ] **Step 2: Write frozen-run and replay-isolation tests**

Assert run rejects a valid experiment_manifest before handler invocation. Assert replay rejects output equal to or nested below the source, executes only in staging, and preserves source hashes.

- [ ] **Step 3: Run focused tests and confirm RED**

Run: .\.venv\Scripts\python.exe -m pytest tests\test_research_registry.py tests\test_experiment_service.py tests\test_archive_service.py -q

- [ ] **Step 4: Implement explicit registry**

Map 0824_EX01 to champion_challenge and resolve all other archives by existing experiment_type. Future protocols may use handler. Reject duplicates, unknown handlers, and ambiguous identities. Never edit historical protocols.

- [ ] **Step 5: Implement archive validation and replay staging**

archive validate --all calls validate_experiment_archive for each tracked directory. Replay snapshots source hashes, copies into staging outside the archive, removes generated completion files only in the copy, runs the handler, verifies source hashes, then publishes atomically.

- [ ] **Step 6: Run focused tests and commit**

Commit: feat: add explicit research handler registry

### Task 5: Migrate supported experiment orchestration

**Files:**
- Create: src/czsc_trader/research/preregistered.py
- Create: src/czsc_trader/research/handlers.py
- Create: src/czsc_trader/research/holdout.py
- Modify: src/czsc_trader/research/registry.py
- Modify: src/czsc_trader/cli/main.py
- Modify: tests/test_experiment_entrypoints.py and runner tests

**Interfaces:**
- Produces a registered handler for every tracked experiment_type plus champion_challenge.
- Produces pure finalizer functions formerly in scripts/run_experiment.py.
- Adapts existing numerical runner signatures without CLI parsing or printing.

- [ ] **Step 1: Change tests to import research.preregistered and confirm RED**

~~~python
from czsc_trader.research import preregistered as entrypoint
~~~

Delete only assertions whose sole purpose is old argparse dispatch. Add registry dispatch coverage for every tracked type.

- [ ] **Step 2: Move preregistered finalizers without CLI code**

Move protocol validation, report rendering, manifest completion, and run_preregistered_* functions. Do not move argparse, cli, stdout printing, or the old if/elif dispatcher.

- [ ] **Step 3: Implement handler adapters**

Normalize run_pre2026_experiment, champion attribution, four-layer, return-only, factor discovery, Optuna, top-three tournament, all 0825 diagnostics, and decision-boundary runner. Each adapter validates its protocol and receives repository-relative paths from RepositoryContext.

- [ ] **Step 4: Move historical holdout orchestration**

Preserve archive validation and report generation as callable research code, mark 2026 as observed history, and remove its argument parser and printing.

- [ ] **Step 5: Run all experiment entrypoint and runner regressions**

Run: .\.venv\Scripts\python.exe -m pytest tests\test_experiment_entrypoints.py tests\test_four_layer_runner.py tests\test_return_only_runner.py tests\test_factor_discovery_runner.py tests\test_optuna_runner.py tests\test_top3_holdout_runner.py -q

- [ ] **Step 6: Commit**

Commit: refactor: migrate experiment orchestration to handlers

### Task 6: Delete legacy CLIs, dead code, and obsolete tests

**Files:**
- Delete: scripts directory
- Modify: runner modules that currently contain argparse/main guards
- Create: tests/test_architecture_boundaries.py
- Delete or modify: tests tied only to deleted parsing paths

**Interfaces:**
- Produces no executable path other than czsc-trader.
- Enforces dependency direction with a static AST test.

- [ ] **Step 1: Write failing architecture tests**

~~~python
def test_no_legacy_python_entrypoints():
    assert not Path("scripts").exists()

def test_runner_modules_have_no_cli():
    for path in Path("src/czsc_trader").rglob("*_runner.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert "argparse" not in imported_modules(tree)
        assert not defines_main_guard(tree)
~~~

Also prohibit Core/domain imports of cli or application, Research imports of cli, and CLI direct imports of concrete runners.

- [ ] **Step 2: Run architecture tests and confirm RED**

Run: .\.venv\Scripts\python.exe -m pytest tests\test_architecture_boundaries.py -q

- [ ] **Step 3: Delete scripts and module CLIs**

Remove argparse imports, parser functions, main wrappers, CLI-only prints, and __main__ guards. Keep only numerical and protocol functions referenced by handlers.

- [ ] **Step 4: Remove unreachable code and obsolete tests**

Use rg for every candidate symbol before deletion. Remove duplicate JSON formatting, old dispatch tests, parser-only fixtures, unused imports, types, constants, and functions. Preserve research behavior assertions under their new imports.

- [ ] **Step 5: Verify collection and boundaries**

Run: .\.venv\Scripts\python.exe -m pytest --collect-only -q
Run: .\.venv\Scripts\python.exe -m pytest tests\test_architecture_boundaries.py tests\test_cli_contract.py tests\test_experiment_entrypoints.py -q

- [ ] **Step 6: Commit**

Commit: refactor: remove legacy command entrypoints

### Task 7: Documentation and final verification

**Files:**
- Modify: docs/RESEARCH_HANDOFF.md
- Modify: every active command document found by rg "scripts[\\/]"
- Modify tests only for genuine contract defects found by final verification

- [ ] **Step 1: Replace active old command examples**

Document only czsc-trader archive validate, data validate/prepare, baseline commands, backtest run, experiment run, and experiment replay. Historical prose may name deleted scripts only as past architecture, never as instructions.

- [ ] **Step 2: Run complete pytest**

Run: .\.venv\Scripts\python.exe -m pytest -q
Expected: zero failures.

- [ ] **Step 3: Compile and validate every archive**

Run: .\.venv\Scripts\python.exe -m compileall -q src tests
Run: .\.venv\Scripts\czsc-trader.exe archive validate --all --repo-root .
Expected: compile exit 0 and all tracked archives PASS.

- [ ] **Step 4: Run fresh 588080 parity backtest**

Run: .\.venv\Scripts\czsc-trader.exe backtest run --symbol 588080.SH --asset etf --start 2026-01-01 --end 2026-08-21 --repo-root .

Expected: return 0.6197253904580182, Sharpe 2.962783088249733, 10 trades, audit PASS, baseline_20260826.

- [ ] **Step 5: Run cleanup checks**

Run rg for deleted script names outside docs/superpowers.
Run rg for argparse and __main__ under src/czsc_trader.
Run git diff --check and git status --short --branch.

Expected: no active old-entry references, no runner CLI remnants, no whitespace errors.

- [ ] **Step 6: Commit documentation**

Commit: docs: hand off unified command architecture

- [ ] **Step 7: Report evidence**

Report commit range, exact test counts, archive validation, fresh output directory, parity metrics, and branch divergence. Do not push unless requested.

