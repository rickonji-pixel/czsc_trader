# CZSC Route Decision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Exhaust the preregistered, interpretable CZSC information space for 588080 and finish with either an audited historical challenger or an audited route-termination decision.

**Architecture:** A reusable route engine will separate signal inventory, exact multi-value representation, family construction, common admission gates, and strategy integration. Each family is frozen in its own experiment directory; a final program ledger decides whether strategy integration is allowed and whether the route passes or terminates.

**Tech Stack:** Python 3.12, pandas, NumPy, CZSC 1.0.1, vectorbt 1.1.0, Optuna, pytest, existing `czsc-trader experiment` and archive infrastructure.

**Spec:** `experiments/0901_EX05/01_goal.md` and `experiments/0901_EX05/02_design.md`

## Global Constraints

- Symbol is exactly `588080.SH`; 2020 is initialization and 2021-2025 is visible evaluation history.
- No 2026 market data may be loaded by discovery, admission, search, or post-failure adjustment.
- The active comparator is exactly `baseline_20260826` from the baseline registry.
- Six information families run in the fixed order from the spec; at most two strategy-integration rounds follow.
- State support is at least 10 active and 10 control days in every year, with activation at most 90%.
- Event support is at least 10 independent events, at least four years, and at least two events per represented year.
- State effects need at least four of five years in one direction; event effects need every supported year in one direction.
- Every admitted factor must pass causal replay, influence robustness, conditional incremental validity, identity, and executability audits.
- A strategy passes only when return is not below the active baseline and maximum drawdown strictly improves at `1e-12` tolerance.
- All outcomes, including empty families, FAIL, ERROR, and route termination, are Git-tracked experiment archives.

---

### Task 1: Repair and lock the research handler contract

**Files:**
- Modify: `src/czsc_trader/research/handlers.py`
- Modify: `tests/test_cli_contracts.py`

**Interfaces:**
- Consumes: `FunctionHandler.run(context, experiment_dir)`.
- Produces: a working `_czsc_factor_stability` adapter and a registered `czsc_route_family_diagnostic` adapter.

- [ ] Write a regression test that monkeypatches `run_czsc_factor_stability_experiment`, calls the resolved handler, and asserts the returned summary is propagated.
- [ ] Run the exact test and confirm the current adapter returns `None`.
- [ ] Restore the missing return call before `_czsc_bi_layer_stability` and remove the unreachable duplicate after `_czsc_incremental_validity`.
- [ ] Add the route-family adapter only after its runner exists; assert every registered function handler returns a mapping in its focused contract test.
- [ ] Run `pytest -q tests/test_cli_contracts.py` and commit the repair independently.

### Task 2: Build the deterministic CZSC signal inventory

**Files:**
- Create: `src/czsc_trader/czsc_route.py`
- Create: `tests/test_czsc_route.py`
- Create: `experiments/0901_EX05/artifacts/protocol.json`

**Interfaces:**
- Produces: `SignalSpec`, `FactorSpec`, `list_czsc_signal_specs()`, `parse_signal_value()`, `classify_signal_family()`, and `inventory_records()`.
- `parse_signal_value(value)` returns exactly three normalized semantic fields while preserving the original string.

- [ ] Test that all names from `czsc._native.list_signal_names()` are either assigned a supported family or recorded with an explicit exclusion reason.
- [ ] Test parsing of one-, two-, and three-value CZSC outputs without collapsing the second or third value.
- [ ] Implement immutable signal/factor specifications and deterministic name sorting.
- [ ] Record callable availability, frequency applicability, default parameters, semantic family, event tokens, and explicit unsupported status.
- [ ] Freeze the inventory protocol hash before any market outcome is evaluated.
- [ ] Run `pytest -q tests/test_czsc_route.py` and commit.

### Task 3: Implement common factor construction and admission gates

**Files:**
- Modify: `src/czsc_trader/czsc_route.py`
- Modify: `src/czsc_trader/czsc_factor_stability.py`
- Modify: `tests/test_czsc_route.py`

**Interfaces:**
- Produces: `build_family_factors(raw, family, protocol) -> (DataFrame, list[dict])` and `admit_factors(candidates, references, outcomes, protocol) -> AdmissionResult`.
- `AdmissionResult` contains `admitted`, `rejected`, `yearly_metrics`, `causal_audit`, `influence_audit`, `redundancy_audit`, and `conditional_audit`.

- [ ] Add fixtures for one stable state, one sparse stable event, one single-outlier event, one duplicate reference, and one cross-year sign reversal.
- [ ] Verify the fixtures fail before the common gate exists.
- [ ] Implement exact state and independent-event support rules from the spec.
- [ ] Implement five-year direction checks, annual leave-one-out checks, per-event deletion checks, and the 50% contribution limits.
- [ ] Implement conditional strata using champion target position plus frozen baseline factors with absolute correlation at least 0.80.
- [ ] Implement deterministic identity and alias fingerprints.
- [ ] Run the focused tests and commit.

### Task 4: Execute atomic structures and sparse events

**Files:**
- Create: `src/czsc_trader/czsc_route_runner.py`
- Modify: `src/czsc_trader/research/handlers.py`
- Modify: `tests/test_czsc_route.py`
- Create and archive: `experiments/0901_EX06/`
- Create and archive: `experiments/0901_EX07/`

**Interfaces:**
- Produces: `run_czsc_route_family(raw_dir, baseline_root, experiment_dir, execution_commit)`.
- EX06 family is `atomic_structure`; EX07 family is `sparse_event`.

- [ ] Test protocol rejection for an unknown family, post-2025 date, missing comparator, or mutable inventory hash.
- [ ] Implement causal signal generation for 30-minute, daily, and weekly configurations and exact daily backward alignment.
- [ ] Pre-register EX06 and EX07 separately before running either family.
- [ ] Run EX06, archive its coverage matrix, admission funnel, metrics, and audits, then commit.
- [ ] Run EX07 with event-onset semantics, archive its event ledger and influence audit, then commit.

### Task 5: Execute dynamics and cross-frequency relations

**Files:**
- Modify: `src/czsc_trader/czsc_route.py`
- Modify: `tests/test_czsc_route.py`
- Create and archive: `experiments/0901_EX08/`
- Create and archive: `experiments/0901_EX09/`

**Interfaces:**
- Produces: `build_dynamic_factors()` and `build_cross_frequency_factors()` using only frozen EX06/EX07 definitions.

- [ ] Test onset, switch, age, decay, and repeated-event construction on fixed categorical sequences.
- [ ] Test same-direction, divergence, and one-completed-period lag relations without forward filling from future bars.
- [ ] Pre-register and execute EX08; archive every dynamic definition and admission outcome.
- [ ] Pre-register and execute EX09; archive every frequency relation and alignment audit.
- [ ] Commit each experiment independently.

### Task 6: Execute champion-error conditioning and eligible interactions

**Files:**
- Modify: `src/czsc_trader/czsc_route.py`
- Modify: `tests/test_czsc_route.py`
- Create and archive: `experiments/0901_EX10/`
- Create and archive: `experiments/0901_EX11/`

**Interfaces:**
- Produces: `build_champion_error_factors()` and `build_pairwise_interactions()`.
- Interactions consume only factor identities admitted by EX06-EX10.

- [ ] Define good and bad future paths from the fixed 20-session return and maximum-drawdown outcomes without using them in real-time factor values.
- [ ] Test that error-path labels are evaluation labels only and cannot appear in the generated factor frame.
- [ ] Pre-register and execute EX10 inside frozen champion-position and reference-factor strata.
- [ ] If no factor has been admitted, archive EX11 as an empty family with reason `no_eligible_parent_factors`; otherwise test and execute all deterministic second-order pairs.
- [ ] Commit each experiment independently.

### Task 7: Decide and run strategy integration

**Files:**
- Create: `src/czsc_trader/czsc_route_strategy_runner.py`
- Create: `tests/test_czsc_route_strategy.py`
- Conditionally create and archive: `experiments/0901_EX12/`
- Conditionally create and archive: `experiments/0901_EX13/`

**Interfaces:**
- Produces: `run_route_strategy_experiment(...)` returning baseline and challenger metrics, orders, events, audits, and a frozen challenger only on PASS.

- [ ] If the admitted-factor ledger is empty, skip directly to Task 8 without creating a synthetic search space.
- [ ] Test unified `0—1` target-position generation, next-open execution, costs, order-event linkage, and exact baseline comparison.
- [ ] Freeze EX12 factor identities, weight bounds, thresholds, seed, objective ordering, and Trial budget before search.
- [ ] Execute EX12 and archive all trials and the best audited candidate.
- [ ] Create EX13 only if EX12 diagnostics identify a preregistrable architecture cause; otherwise record why round two is forbidden.
- [ ] If EX13 is allowed, freeze its one permitted architecture change, execute it, and archive the result.

### Task 8: Publish the terminal route decision

**Files:**
- Complete and archive: `experiments/0901_EX05/03_execution.md`
- Complete and archive: `experiments/0901_EX05/04_conclusion.md`
- Create and archive: `experiments/0901_EX14/`
- Modify: `docs/RESEARCH_HANDOFF.md`

**Interfaces:**
- Produces: the final coverage matrix, admission funnel, terminal decision, and either a frozen historical challenger or an explicit CZSC route-termination report.

- [ ] Verify every inventory item is covered or has an explicit exclusion reason and every information family has a frozen archive.
- [ ] Verify no artifact hashes 2026 market inputs and no strategy file exists unless both hard performance gates and all audits pass.
- [ ] Aggregate rejection reasons and distinguish “current finite CZSC route exhausted” from universal claims about all possible CZSC ideas.
- [ ] Write and archive the terminal decision and update the cross-machine handoff.
- [ ] Run `pytest -q`, `czsc-trader archive validate --all`, `python -m compileall -q src tests`, and `git diff --check`.
- [ ] Commit the final archive and leave the research branch unmerged and unpushed for user review.

