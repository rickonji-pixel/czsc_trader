# Project Cleanup and Market Data Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refresh all tracked ETF data through 2026-09-01 and remove retired experiment execution code, paths, dependencies, and development tests while preserving audited backtesting and immutable archive validation.

**Architecture:** Treat the supported CLI surface as the production root and retain only its transitive source dependencies. Keep `experiments/` as immutable data, but delete experiment run/replay infrastructure and historical research implementations. Replace development tests with one installed-CLI end-to-end contract.

**Tech Stack:** Python 3.12, argparse, pandas, CZSC, vectorbt, Plotly, czsc-dataflows, pytest, Git.

**Spec:** `docs/superpowers/specs/2026-09-01-project-cleanup-design.md`

## Global Constraints

- Work on `codex/0901-project-cleanup`; do not use a Git worktree.
- Use Tushare post-adjusted (`hfq`) prices for all four tracked ETFs.
- Do not modify any file under `experiments/`.
- Preserve active baseline `baseline_20260901`, candidate 143, without numerical changes.
- Retain only `data`, `baseline`, `backtest`, and `archive` CLI resource groups.
- Keep one network-free end-to-end test file.
- Use minimal verification rather than the retired slow test suite.

---

### Task 1: Refresh all tracked ETF data

**Files:**
- Modify: `data/raw/159352_*`
- Modify: `data/raw/159516_*`
- Modify: `data/raw/515050_*`
- Modify: `data/raw/588080_*`

**Interfaces:**
- Consumes: installed `czsc-trader data prepare` and existing manifest start dates.
- Produces: validated 30-minute, daily, weekly, manifest, and validation files through 2026-09-01.

- [ ] **Step 1: Capture current data identities**

Run:

```powershell
.\.venv\Scripts\czsc-trader.exe data validate --symbol 159352.SZ
.\.venv\Scripts\czsc-trader.exe data validate --symbol 159516.SZ
.\.venv\Scripts\czsc-trader.exe data validate --symbol 515050.SH
.\.venv\Scripts\czsc-trader.exe data validate --symbol 588080.SH
```

Expected: all four commands PASS and show their current cutoffs.

- [ ] **Step 2: Publish refreshed post-adjusted data**

Run sequentially to respect the remote data source:

```powershell
.\.venv\Scripts\czsc-trader.exe data prepare --symbol 159352.SZ --asset etf --start 2025-01-01 --end 2026-09-01
.\.venv\Scripts\czsc-trader.exe data prepare --symbol 159516.SZ --asset etf --start 2025-01-01 --end 2026-09-01
.\.venv\Scripts\czsc-trader.exe data prepare --symbol 515050.SH --asset etf --start 2024-01-02 --end 2026-09-01
.\.venv\Scripts\czsc-trader.exe data prepare --symbol 588080.SH --asset etf --start 2020-01-01 --end 2026-09-01
```

Expected: every command returns PASS with `adjustment.mode=hfq`.

- [ ] **Step 3: Verify published cutoffs and file scope**

Run the four validation commands again, inspect every manifest's three frequency
last timestamps, and run `git status --short`.

Expected: all frequencies end on 2026-09-01 and only `data/raw` files changed.

- [ ] **Step 4: Commit the data publication**

```powershell
git add data/raw
git commit -m "data: refresh tracked ETFs through 2026-09-01"
```

### Task 2: Retire experiment execution with a failing CLI contract

**Files:**
- Modify: `tests/test_cli_e2e.py`
- Modify: `src/czsc_trader/cli/main.py`
- Delete: `src/czsc_trader/application/experiment_service.py`
- Delete: `src/czsc_trader/research/`

**Interfaces:**
- Consumes: installed `czsc-trader --help`.
- Produces: a CLI exposing exactly `data`, `baseline`, `backtest`, and `archive`.

- [ ] **Step 1: Add the failing public-surface test**

Add a subprocess test that runs `czsc-trader --help`, asserts return code zero,
asserts all four retained resource names are present, and asserts `experiment` is
absent.

- [ ] **Step 2: Run the test and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_cli_e2e.py::test_cli_exposes_only_supported_resources -q
```

Expected: FAIL because `experiment` is still printed.

- [ ] **Step 3: Remove experiment run/replay from the CLI**

Delete experiment-service and registry imports, `_experiment_run`,
`_experiment_replay`, and the `experiment` parser block from `cli/main.py`.
Delete `application/experiment_service.py` and the `research/` package.

- [ ] **Step 4: Run the test and verify GREEN**

Run the same focused test.

Expected: PASS.

### Task 3: Delete unreachable historical research code and dependencies

**Files:**
- Delete: historical research-only modules under `src/czsc_trader/`
- Modify: `pyproject.toml`
- Delete: `docs/superpowers/plans/`
- Delete: `docs/superpowers/specs/`

**Interfaces:**
- Consumes: retained application services as dependency roots.
- Produces: a source tree containing only modules reachable from supported commands.

- [ ] **Step 1: Compute and review the retained import closure**

Use Python AST imports rooted at data, baseline, backtest, archive services and CLI
output. Add package initializers and the edited CLI main explicitly. Confirm each
deletion candidate is unreachable from those roots.

- [ ] **Step 2: Delete unreachable source modules**

Use `apply_patch` to delete all historical attribution, diagnostics, search,
position, risk, CZSC route/stability, tournament, and runner modules outside the
retained closure. Do not delete `experiment_archive.py` or any active baseline
dependency.

- [ ] **Step 3: Remove retired dependencies**

Delete `joblib==1.5.3` and `optuna==4.9.0` from root `pyproject.toml`. Verify no
retained source file imports either package.

- [ ] **Step 4: Delete superseded planning paths and local caches**

Use `apply_patch` to delete every Git-tracked file under `docs/superpowers/plans`
and `docs/superpowers/specs`. Remove untracked `.pytest_cache` and
`scripts/__pycache__` with explicit PowerShell literal paths after confirming they
are inside the repository.

- [ ] **Step 5: Compile retained source**

```powershell
.\.venv\Scripts\python.exe -m compileall -q src packages/dataflows/src
```

Expected: exit code 0.

### Task 4: Consolidate the end-to-end test and documentation

**Files:**
- Modify: `tests/test_cli_e2e.py`
- Delete: every other `tests/test_*.py`
- Modify: `README.md`
- Modify: `docs/RESEARCH_HANDOFF.md`

**Interfaces:**
- Consumes: installed CLI and local Git-tracked artifacts.
- Produces: one network-free end-to-end test and current usage documentation.

- [ ] **Step 1: Consolidate stable contracts into `test_cli_e2e.py`**

Retain the package-import and fixed backtest checks. Add installed-CLI subprocess
checks for the four local data validations, candidate-143 baseline validation, and
all-archive validation. Parse JSON output and assert the current identities.

- [ ] **Step 2: Delete development tests**

Delete all other files under `tests/`. Confirm `git ls-files tests` returns only
`tests/test_cli_e2e.py`.

- [ ] **Step 3: Update usage documentation**

Rewrite README and handoff command/path sections to list only the four supported
resource groups, immutable experiment archives, and the single end-to-end test.
Remove instructions for experiment run/replay and historical runner modules.

- [ ] **Step 4: Run the consolidated test**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_cli_e2e.py -q
```

Expected: all tests PASS without network access.

### Task 5: Verify and commit the cleanup

**Files:**
- Verify: all retained code, configuration, documentation, tests, and archives.

**Interfaces:**
- Consumes: completed Tasks 1-4.
- Produces: clean, committed feature branch ready for integration.

- [ ] **Step 1: Refresh editable package metadata and dependencies**

```powershell
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
.\.venv\Scripts\python.exe -m pip check
```

Expected: installation succeeds and `pip check` reports no broken requirements.

- [ ] **Step 2: Run all acceptance commands**

Run compileall, the consolidated test, all four data validations,
`baseline validate --version baseline_20260901 --symbol 588080.SH`,
`archive validate --all`, and the fixed backtest through 2026-08-21.

Expected: every command exits 0; baseline candidate is 143; archive count is 46;
fixed strategy return remains `0.7528525916956634`.

- [ ] **Step 3: Audit deletions and repository boundaries**

Run `git diff --check`, `git status --short`, retained import closure analysis,
`rg` for `experiment run`, `experiment replay`, `optuna`, and `joblib`, and confirm
no `experiments/` files changed.

Expected: no stale supported instructions or imports, no archive modifications,
and no whitespace errors.

- [ ] **Step 4: Commit the cleanup**

```powershell
git add -A
git commit -m "refactor: retire historical experiment runtime"
```

- [ ] **Step 5: Verify final branch state**

Run `git status --short --branch` and `git log -3 --oneline --decorate`.

Expected: clean `codex/0901-project-cleanup` branch with separate design, data, and
cleanup commits.
