# CZSC BI Layer Stability Implementation Plan

> **For agentic workers:** Execute in the main session only. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Test whether the second-value BI power layer adds stable information beyond the first-value BI direction.

**Architecture:** Parse the exact two-value CZSC signal into ten joint states, evaluate each against same-direction controls, freeze discovery candidates, and replay only those candidates in locked validation.

**Tech Stack:** Python 3.12, pandas, NumPy, existing CZSC generator and research registry.

**Spec:** `experiments/0901_EX02/02_design.md`

## Global Constraints

- Candidate space is exactly ten direction-by-layer states.
- Controls must share the candidate's direction.
- Discovery ends at 2023-12-31 and validation ends at 2025-12-31.
- No 2026 access or strategy optimization.

---

### Task 1: Multi-value factor construction

- [ ] Write failing tests for exact v1/v2 parsing and same-direction controls.
- [ ] Implement joint factor construction and conditional stability evaluation.
- [ ] Verify the focused tests pass.

### Task 2: Registered experiment execution

- [ ] Add a failing handler registration test.
- [ ] Implement discovery freeze, validation support gates, causal replay, and redundancy audit.
- [ ] Execute the committed protocol and inspect every frozen candidate.

### Task 3: Archive and verification

- [ ] Write execution and conclusion documents.
- [ ] Validate all experiment hashes.
- [ ] Run focused and CLI contract tests.
- [ ] Commit without merging or pushing.

