# CZSC State Age Diagnostic Implementation Plan

> **For agentic workers:** Execute in the main session only. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Determine whether the provisional upward-BI risk state is explained by its causal consecutive age.

**Architecture:** Convert the causal upward-state indicator to consecutive age, calculate yearly rank relationships and fixed-bin summaries, and archive a diagnostic classification without strategy optimization.

**Tech Stack:** Python 3.12, pandas, NumPy, SciPy through pandas, existing CZSC generator.

**Spec:** `experiments/0901_EX03/02_design.md`

## Global Constraints

- All 2021-2025 data is visible diagnostic evidence.
- Fixed age bins are 1-3, 4-8, and 9+.
- Primary endpoint is 20-day maximum drawdown.
- Status is COMPLETE, never PASS; 2026 is inaccessible.

---

### Task 1: Causal state age

- [ ] Write a failing test for consecutive state age reset and increment.
- [ ] Implement the pure age and diagnostic summary functions.
- [ ] Verify focused tests pass.

### Task 2: Registered diagnostic

- [ ] Add a failing handler test.
- [ ] Implement and run the committed diagnostic protocol.
- [ ] Inspect yearly correlations and fixed bins.

### Task 3: Archive

- [ ] Write the evidence-bounded conclusion and handoff update.
- [ ] Validate all archives and focused CLI tests.
- [ ] Commit without merging or pushing.

