# Dynamic Position Sizing Experiment Implementation Plan

> **For agentic workers:** Execute each task in order and preserve the preregistration-before-results boundary.

**Goal:** Build, execute, and archive the preregistered `0829_EX02` dynamic full/half-position challenge for `588080.SH`.

**Architecture:** Reuse the active baseline's frozen score and entry/exit thresholds. Add a pure three-state holding machine, causal resize events, and a dedicated formal runner. Keep the original return-only champion challenge unchanged and evaluate exactly one fixed challenger.

**Tech Stack:** Python 3.12, pandas, NumPy, vectorbt, pytest, existing `czsc-trader` CLI and experiment archive framework.

**Spec:** `docs/superpowers/specs/2026-08-29-dynamic-position-sizing-experiment-design.md`

## Task 1: Preregister before real-data evaluation

- Commit the goal, design, protocol, implementation plan and this engineering plan.
- Do not evaluate the dynamic target on repository market data before that commit exists.

## Task 2: Dynamic state machine and events (TDD)

**Files:** `tests/test_dynamic_position_sizing.py`, `src/czsc_trader/position_sizing.py`

- First add failing tests proving full entry, no reduction before minimum hold, `1 -> 0.5`, `0.5 -> 1`, and full exit.
- Implement the minimal pure state machine with only the frozen `0.175/0.025` thresholds and `0.5/1.0` positions.
- Emit exact `Entry/Reduce/Increase/Exit` events with before/after positions.

## Task 3: Causal resize execution and audit (TDD)

**Files:** `tests/test_dynamic_position_sizing.py`, `src/czsc_trader/backtest.py`, `src/czsc_trader/audit.py`

- First add a failing period-backtest test covering Sell/Buy resize orders and event provenance.
- Match Buy to `Entry/Increase`, Sell to `Reduce/Exit`, using the target transition to require one exact event.
- Audit event direction, next-open timing, score provenance and exact before/after positions.
- Preserve existing binary and entry-fixed behavior.

## Task 4: Dedicated formal runner and handler (TDD)

**Files:** `tests/test_dynamic_position_sizing.py`, `src/czsc_trader/dynamic_position_sizing_runner.py`, `src/czsc_trader/research/handlers.py`

- First add failing tests for exact protocol validation, handler resolution and unchanged three-window PASS rule.
- Evaluate the single fixed challenger on the eight visible half-years, then freeze it before loading 2026.
- Evaluate the original three 2026 windows and write PASS/FAIL/ERROR archives truthfully.

## Task 5: Formal execution and delivery

- Commit the tested implementation before running `experiment run`.
- Execute `0829_EX02` once through the unified CLI.
- Update the archive expectation and `docs/RESEARCH_HANDOFF.md` with exact facts.
- Run focused tests, archive validation, full pytest, pip/data/baseline checks and `git diff --check`.
- Commit and push `codex/dynamic-position-sizing-experiment`; do not promote the challenger automatically.
