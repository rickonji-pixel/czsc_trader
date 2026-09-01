# Project Cleanup and Market Data Refresh Design

## Objective

Refresh every tracked market-data set through the latest completed trading day,
then reduce the repository to the production capabilities that remain useful:
market-data preparation and validation, baseline management, audited backtesting,
and immutable experiment-archive validation.

The cleanup deliberately retires historical experiment execution and replay.
Existing experiment directories remain Git-tracked, readable, and hash-validatable,
but their dedicated implementation modules are no longer maintained as runtime
features.

## Scope

### Market data

Refresh these four tracked ETFs through 2026-09-01:

- `588080.SH`, starting from the existing manifest start date `2020-01-01`;
- `159352.SZ`, starting from `2025-01-01`;
- `159516.SZ`, starting from `2025-01-01`;
- `515050.SH`, starting from `2024-01-02`.

The existing `data prepare` path remains the only publisher. It must continue to
use Tushare post-adjusted (`hfq`) prices and must publish 30-minute, daily, weekly,
manifest, and validation files atomically. Every symbol must pass `data validate`
after publication. The published last timestamp must be 2026-09-01 for all three
frequencies; otherwise the refresh is incomplete and must be reported rather than
silently treated as current.

### Supported command-line surface

The installed `czsc-trader` command retains exactly these resource groups:

- `data prepare` and `data validate`;
- `baseline list`, `baseline show`, and `baseline validate`;
- `backtest run`;
- `archive validate`.

The `experiment run` and `experiment replay` commands are removed. Their
application service, handler registry, contracts, and historical protocol adapters
are removed with them.

### Runtime source tree

Retained source code is the import closure of the supported CLI surface, plus
package initializers. In particular, current baseline execution keeps the factor,
rule, four-layer, regime-weight, data, audit, charting, reporting, and backtest
modules it actually imports.

Historical research-only modules are deleted when they are unreachable from the
supported CLI surface. This includes dedicated attribution, Optuna search,
position-sizing, risk, CZSC diagnostic, route, tournament, and experiment runner
modules. Git history and the immutable `experiments/` artifacts remain the record
of those studies.

Dependencies used only by retired research modules, specifically `optuna` and
`joblib`, are removed from the root package requirements. Dependencies required by
the retained runtime or the independently packaged `czsc-dataflows` component stay
unchanged.

### Repository paths and documentation

The following path roles remain authoritative:

- `data/raw/`: published market data only;
- `configs/`: backtest windows and frozen baseline registry;
- `experiments/`: immutable research archives only;
- `outputs/`: ignored ordinary backtest output only;
- `src/czsc_trader/`: supported runtime implementation;
- `tests/`: minimal end-to-end verification only.

Historical planning material under `docs/superpowers/plans/` and
`docs/superpowers/specs/` is removed after this design has been implemented. The
repository history preserves those files, while each completed experiment keeps
its own goal, design, execution, conclusion, protocol, results, and manifest.
`README.md` and `docs/RESEARCH_HANDOFF.md` are updated to describe only supported
commands and paths.

Empty or generated local paths such as `scripts/__pycache__` and `.pytest_cache`
are removed locally and remain untracked.

## Test policy

All development-oriented unit and TDD test files are replaced by one focused
end-to-end test file. It verifies only stable user-visible contracts:

1. the installed CLI exposes exactly the four supported resource groups;
2. the installed `czsc-dataflows` package imports outside the checkout;
3. all four tracked symbols pass local data validation;
4. the active baseline resolves to candidate 143 in `baseline_20260901`;
5. a fixed-date `588080.SH` backtest returns the frozen audited result and writes
   its audit and report artifacts;
6. every Git-tracked experiment archive passes immutable-manifest validation.

The test suite does not fetch network data and does not retest retired research
algorithms. Market-data refresh is an explicit operational command followed by
validation, not a test fixture.

## Execution sequence

1. Refresh and validate all four symbols, then commit only the published
   `data/raw` changes.
2. Remove retired CLI commands, source modules, dependencies, documentation paths,
   and TDD tests; consolidate the end-to-end contract.
3. Update `README.md` and `docs/RESEARCH_HANDOFF.md`.
4. Run compile checks, `pip check`, the consolidated end-to-end test, four data
   validations, active baseline validation, all-archive validation, and the fixed
   backtest.
5. Commit the cleanup separately from the data refresh so both changes remain
   auditable.

## Acceptance criteria

- All four symbols are published and validated through 2026-09-01 using `hfq`.
- `czsc-trader --help` exposes only `data`, `baseline`, `backtest`, and `archive`.
- No retained production module imports a deleted historical research module.
- Root package metadata no longer requires `optuna` or `joblib`.
- Only the consolidated end-to-end test file remains under `tests/`, and it passes.
- The active baseline remains `baseline_20260901`, candidate 143.
- All 46 existing experiment archives validate without modification.
- A fixed-date audited backtest remains numerically unchanged.
- The worktree contains no generated cache files or uncommitted changes at
  delivery.

## Non-goals

- Reproducing or rerunning historical experiment algorithms;
- deleting or rewriting any existing experiment archive;
- changing candidate 143, its weights, regime definition, or threshold;
- changing backtest accounting, chart behavior, or output naming;
- adding automated network refreshes or scheduled jobs.
