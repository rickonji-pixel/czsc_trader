# CZSC Incremental Validity Diagnostic Implementation Plan

> **For agentic workers:** Execute in the main session only. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Isolate whether cxt_bi_zdf valid output adds risk information beyond the champion daily BI direction.

**Architecture:** Generate the new and reference signals causally, compare valid-output states only inside matching reference direction, and report cross-year conditional effects without strategy selection.

**Tech Stack:** Python 3.12, pandas, existing CZSC generator and stability diagnostics.

**Spec:** `experiments/0901_EX04/02_design.md`

## Global Constraints

- Exactly two symmetric candidates: upward and downward.
- Controls share the champion daily BI direction.
- 2021-2025 is visible diagnostic history; 2026 is inaccessible.
- Status is COMPLETE and no strategy is produced.

---

### Task 1: Registered diagnostic

- [ ] Add a failing handler registration test.
- [ ] Implement exact conditional paths, support, correlations, and causal replay.
- [ ] Run the committed protocol.

### Task 2: Archive

- [ ] Inspect both yearly paths and write the conclusion.
- [ ] Update the research handoff.
- [ ] Validate archives and focused CLI tests.
- [ ] Commit without merging or pushing.

