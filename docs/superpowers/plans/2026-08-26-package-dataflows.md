# Independent Dataflows Package Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the market-data adapters into an independently installable `dataflows` subproject while preserving the installed `czsc-trader` command and all research behavior.

**Architecture:** Build `packages/dataflows` as a standalone src-layout distribution with no dependency on `czsc_trader`. Install the subproject and root application together from relative checkout paths, keep the stable `dataflows.*` import namespace, and pass the repository-root `.env` path from the application boundary.

**Tech Stack:** Python 3.12, setuptools, pip editable installs, pandas, Tushare, stockstats, python-dotenv, pytest.

**Spec:** `docs/superpowers/specs/2026-08-26-independent-dataflows-package-design.md`

## Global Constraints

- Preserve the public `czsc-trader` command tree, raw-data schema, frozen strategies, experiment archives, and research conclusions.
- `packages/dataflows` must not import `czsc_trader`.
- Preserve process-level `TUSHARE_TOKEN` precedence.
- Move the ignored credential file without reading or printing its contents.
- Do not embed any absolute checkout path in package metadata.
- Keep only end-to-end tests, in line with the repository testing policy.

---

### Task 1: Define the independent package acceptance boundary

**Files:**
- Modify: `tests/test_cli_e2e.py`

**Interfaces:**
- Consumes: the active virtual environment's Python executable
- Produces: an end-to-end assertion that `import dataflows` works outside the checkout and that the module is loaded from `packages/dataflows`

- [x] **Step 1: Add the failing packaging acceptance test**

Add a test that starts `sys.executable` with `cwd=tmp_path`, imports `dataflows`, resolves `dataflows.__file__`, and asserts the path contains `packages/dataflows`.

- [x] **Step 2: Run the focused test and verify the old layout fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_cli_e2e.py -k dataflows_package -q`

Expected: FAIL because the current editable installation exposes only `czsc_trader.dataflows`.

### Task 2: Build and wire the standalone distribution

**Files:**
- Create: `packages/dataflows/pyproject.toml`
- Move: `src/czsc_trader/dataflows/*.py` to `packages/dataflows/src/dataflows/*.py`
- Move: `dataflows/README.md` to `packages/dataflows/README.md`
- Modify: `pyproject.toml`
- Modify: `src/czsc_trader/market_data_prep.py`

**Interfaces:**
- Consumes: `czsc-dataflows==0.1.0` as an installed distribution
- Produces: stable imports `dataflows.tushare_stock` and `dataflows.tushare_etf`

- [x] **Step 1: Create subproject metadata**

Define a setuptools src-layout project named `czsc-dataflows`, version `0.1.0`, requiring Python 3.12 and declaring `pandas>=2.0.0`, `tushare>=1.4.29`, `stockstats>=0.6.5`, and `python-dotenv>=1.0.0`.

- [x] **Step 2: Move all adapter modules into the independent package**

Preserve relative imports inside the package and replace the temporary application imports `czsc_trader.dataflows.*` with `dataflows.*`.

- [x] **Step 3: Declare the application dependency**

Add `czsc-dataflows==0.1.0` to the root project dependencies. Use the portable bootstrap command `.\.venv\Scripts\python.exe -m pip install -e .\packages\dataflows -e ".[test]"`; do not add a `file://` dependency.

- [x] **Step 4: Reinstall both editable projects and pass the acceptance test**

Run the bootstrap command, then run `.\.venv\Scripts\python.exe -m pytest tests/test_cli_e2e.py -k dataflows_package -q`.

Expected: PASS, with `dataflows.__file__` resolving below `packages/dataflows/src/dataflows`.

### Task 3: Move credentials and remove the ambiguous root directory

**Files:**
- Move: `dataflows/.env` to `.env` without reading it
- Move: `dataflows/.env.example` to `.env.example`
- Delete: `dataflows/requirements-dataflows.txt`
- Modify: `.gitignore`
- Modify: `src/czsc_trader/application/data_service.py`
- Modify: `packages/dataflows/src/dataflows/config.py`

**Interfaces:**
- Consumes: explicit `env_file: str | Path | None`
- Produces: repository credential path `RepositoryContext.root / ".env"`

- [x] **Step 1: Protect the destination and move the ignored credential**

Confirm root `.env` does not exist, move `dataflows/.env` with PowerShell `Move-Item -LiteralPath`, and change `.gitignore` from `dataflows/.env` to `/.env`.

- [x] **Step 2: Move the example and fold dependency documentation into package metadata**

Move `.env.example` to the root, update its copy instruction, and remove the superseded requirements file only after every dependency is represented by a `pyproject.toml`.

- [x] **Step 3: Make configuration repository-independent**

Pass `context.root / ".env"` from the application. In the library, remove the repository-relative default token file: an explicit path or process `TUSHARE_TOKEN` is required.

- [x] **Step 4: Confirm the old root directory is gone**

Run: `Test-Path -LiteralPath dataflows`

Expected: `False`.

### Task 4: Update operator documentation and verify the complete tree

**Files:**
- Modify: `README.md`
- Modify: `packages/dataflows/README.md`
- Modify: `docs/RESEARCH_HANDOFF.md`
- Modify: `docs/superpowers/plans/2026-08-26-package-dataflows.md`

**Interfaces:**
- Documents: the two-project bootstrap command, package boundary, root `.env`, and stable `dataflows.*` imports
- Preserves: every tracked file under `data/raw` and `experiments`

- [x] **Step 1: Update documentation and remove stale paths**

Document `packages/dataflows`, `.env`, and the dual editable install. Search active runtime code and current usage documents for `czsc_trader.dataflows`, `dataflows/.env`, and `requirements-dataflows.txt`; require no matches outside historical plans/specifications.

- [x] **Step 2: Run the real installed data preparation entry without `PYTHONPATH`**

Run `czsc-trader data prepare` for `588080.SH` through 2026-08-25 using the actual repository root. Require `PASS`, then restore any generated-only manifest timestamps so tracked market data remains unchanged.

- [x] **Step 3: Run minimal full regression**

Run `.\.venv\Scripts\python.exe -m pytest -q`, compile both source trees, run `pip check`, and run `.\.venv\Scripts\czsc-trader.exe archive validate --all --repo-root .`.

Expected: all end-to-end tests pass, compilation succeeds, `pip check` reports no broken requirements, and all 15 experiment archives validate.

- [x] **Step 4: Audit the final diff**

Run `git diff --check`, `git status --short --branch`, and `git diff -- data/raw experiments`. Require no raw-data or experiment changes and mark every plan checkbox complete.
