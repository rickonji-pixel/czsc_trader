# Code Quality and Runtime Efficiency Design

## 1. Goal

Improve the maintainability and routine execution speed of `czsc_trader` while
preserving the active baseline, trading decisions, research archives, market
data identity, and existing command-line interfaces.

The work has four measurable outcomes:

1. all strategies shown in one comparison table use one Sharpe-ratio formula;
2. lightweight CLI commands do not import the backtest stack;
3. the default test suite completes in less than 20 seconds on the current
   Windows development machine;
4. static quality checks run without adding a runtime dependency.

## 2. Current Evidence

The clean `master` checkout produced the following measurements on 2026-09-02:

- `25 passed in 48.42s`;
- repeated CLI subprocess tests account for most of the runtime;
- importing `czsc_trader.cli.main` takes about 3.66 seconds because all command
  services are imported eagerly;
- one profiled 2026-06-01 through 2026-08-31 backtest spends 3.72 seconds after
  imports: 1.46 seconds generating factors, 0.95 seconds running the three
  portfolio comparisons, and 0.40 seconds generating the primary chart;
- active baseline, BuyHold, and MA5/MA20 use vectorbt's Sharpe calculation,
  while the execution-policy row uses a separate 252-session formula;
- `tests/test_cli_e2e.py` mixes unit, repository-invariant, integration, and
  installed-entry-point tests in one file;
- Ruff is not installed, while `pip check` and Python bytecode compilation pass.

The factor engine and portfolio simulation are useful work and are not the
first optimization target. Persistent factor caches and incremental CZSC state
would introduce invalidation and reproducibility risk disproportionate to the
measured gain.

## 3. Scope and Non-goals

### In scope

- centralize comparison Sharpe calculation;
- lazy-load command services;
- avoid loading the data-download stack for `data validate`;
- separate tests by responsibility and reduce subprocess coverage;
- move backtest report rendering into the existing reporting package;
- add Ruff as an optional development/test dependency;
- document exact fast and full verification commands.

### Out of scope

- changing the active baseline or its factor, weight, score, threshold, regime,
  or target-position logic;
- changing order timing, fees, execution-policy fills, portfolio equity,
  returns, drawdowns, Calmar ratios, or win/loss ratios;
- adding factor caches, parallelism, databases, or new runtime dependencies;
- changing raw market data, experiment evidence, baseline identities, archive
  identities, or the repository LF/raw-byte contract;
- removing supported CLI commands or changing their JSON result shapes except
  for the explicitly versioned Sharpe values.

## 4. Unified Comparison Metrics

`src/czsc_trader/strategy_metrics.py` will own a public helper with the logical
interface:

```python
def annualized_sharpe(
    equity: pd.Series,
    init_cash: float,
    *,
    annualization: float = 252.0,
) -> float | None:
    ...
```

The calculation will reconstruct the first session return relative to
`init_cash`, append subsequent `equity.pct_change()` values, and compute:

```text
sqrt(252) * mean(daily_returns) / sample_std(daily_returns, ddof=1)
```

The risk-free rate is fixed at zero. Fewer than two finite returns or zero
volatility produces `None`. All four report rows call this helper from their
independently funded equity series. `policy_metrics` no longer contains a
second Sharpe implementation.

The manifest metrics schema changes from legacy version 2 to version 4. Version
4 adds `win_loss_ratio_status` with `VALID`, `NO_LOSSES`, `NO_WINS`, and
`NO_CLOSED_TRADES`, so a missing numeric ratio retains its cause. Reports render
these states as a number, `无亏损`, `无盈利`, or `无闭合交易`. Return,
maximum drawdown, Calmar ratio, win/loss ratio, orders, positions, and equity
remain byte-for-byte or numerically unchanged as appropriate. Existing frozen
experiment artifacts remain immutable and retain their historical metrics.

## 5. CLI Import Boundaries

`src/czsc_trader/cli/main.py` will import only parser, output, error, and context
primitives at module load time. Each command handler will import its application
service when invoked. Parser construction and `--help` therefore remain light.

`src/czsc_trader/application/data_service.py` will import
`prepare_market_data` only inside `prepare_data`; `validate_data` will depend
only on the local data loader. Public command names, arguments, exit codes, JSON
shapes, and error handling remain unchanged.

Success is measured separately:

- importing `czsc_trader.cli.main` should take less than 1.0 second on the
  current machine;
- the default test suite should take less than 20 seconds;
- no runtime target is imposed on the actual backtest because its measured
  work is dominated by required factor and portfolio calculations.

## 6. Test Architecture

Tests will be organized by responsibility:

- `tests/test_strategy_metrics.py`: unified Sharpe and comparison-metric edges;
- `tests/test_execution_policy.py`: price rounding, retry, selection, and advice
  mapping units;
- `tests/test_identity_and_archives.py`: line-ending identity and small archive
  fixtures;
- `tests/test_robustness.py`: CSCV, cyclic shifts, DSR, and parameter geometry;
- `tests/test_repository_contract.py`: active identities, tracked data, CLI
  resource surface, and environment invariants using in-process services;
- `tests/test_cli_e2e.py`: one installed CLI backtest that proves packaging,
  dispatch, audited artifacts, charts, and report output end to end.

The installed daily-advice subprocess test is removed because advice mapping is
covered directly and the installed backtest already proves the console entry
point. Four tracked symbols are validated in one in-process test so heavy
imports occur once.

Validation of all frozen experiment archives remains available as an explicit
repository audit test marked `archive`. The default suite excludes this marker;
release, merge, and archive-changing work runs it explicitly. A small archive
fixture remains in the default suite to protect hashing and cache-exclusion
behavior.

Commands:

```powershell
# routine development
python -m pytest -q

# frozen evidence audit
python -m pytest -q -m archive

# merge verification
python -m pytest -q
python -m pytest -q -m archive
python -m ruff check src tests
```

## 7. Module Responsibility

Backtest report Markdown construction moves from `backtest_runner.py` to
`src/czsc_trader/reporting/backtest_report.py`. The new module accepts already
computed metrics and chart filenames and performs no strategy calculation or
filesystem mutation. `backtest_runner.py` retains causal orchestration and
artifact publication.

No broad file split is planned. The metric and report extractions address the
two responsibilities currently causing correctness and maintenance friction.

## 8. Static Quality Gate

Ruff is added to the `test` optional dependency and configured in
`pyproject.toml` for Python 3.12. The initial rule set covers Pyflakes and the
core pycodestyle error family. Formatting is not made a mandatory gate in this
iteration, avoiding a repository-wide mechanical rewrite.

Ruff must pass for `src` and `tests`. It remains a development dependency and
does not change the installed runtime environment.

## 9. Verification and Acceptance

Implementation is accepted when all of the following hold:

1. identical equity series produce identical Sharpe values for every strategy
   path;
2. the generated comparison report contains the unified values, explicit
   win/loss availability labels, and manifest schema version 4;
3. existing backtest return, drawdown, Calmar, win/loss, order, and equity
   assertions still pass;
4. `czsc-trader --help` exposes the same five resources;
5. the default suite completes in less than 20 seconds on the current machine;
6. the explicit archive suite validates all tracked frozen archives;
7. Ruff, `pip check`, and Python compilation pass;
8. `git diff --check` reports no whitespace errors;
9. no active baseline, execution-policy, raw-data, or experiment artifact file
   changes.

## 10. Delivery

Work is performed on `codex/code-quality-performance`. The design, implementation
plan, implementation, and verification evidence are committed separately. The
branch is not merged or pushed until the user reviews the final results.
