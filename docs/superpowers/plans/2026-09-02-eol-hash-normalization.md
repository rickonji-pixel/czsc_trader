# EOL and Portable Identity Hash Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Standardize repository text to LF and make all strategy identity hashes portable across line endings and operating systems.

**Architecture:** Centralize semantic JSON, normalized text, and raw-byte SHA-256 functions in one identity module. Route baseline, execution-policy, and experiment-archive verification through the appropriate function while preserving raw market-data hashes.

**Tech Stack:** Git attributes, Python 3.12, hashlib, json, pytest.

**Spec:** `docs/superpowers/specs/2026-09-02-eol-hash-normalization-design.md`

## Global Constraints

- Work only in `codex/eol-hash-normalization`; do not use worktrees or subagents.
- Do not modify historical experiment contents, raw market data, state, or outputs.
- Preserve strategy behavior and existing baseline version identities.
- Use focused tests and portable checkout verification.

---

### Task 1: Shared portable identity functions

**Files:**
- Create: `src/czsc_trader/identity.py`
- Modify: `tests/test_cli_e2e.py`

**Interfaces:**
- `canonical_json_sha256(value: Mapping | Path) -> str`
- `normalized_text_sha256(path: Path) -> str`
- `raw_file_sha256(path: Path) -> str`

- [ ] Write failing tests showing JSON identity survives EOL, formatting, and key-order changes.
- [ ] Write failing tests showing text identity survives LF/CRLF/CR while raw identity changes.
- [ ] Implement the three minimal functions and run focused tests.

### Task 2: Baseline and execution-policy migration

**Files:**
- Modify: `src/czsc_trader/baselines.py`
- Modify: `src/czsc_trader/execution_policies.py`
- Modify: `configs/rule_baselines/registry.json`
- Modify: `configs/execution_policies/registry.json`
- Modify: `tests/test_cli_e2e.py`

**Interfaces:**
- Baseline and execution JSON identities consume `canonical_json_sha256`.
- JSON source verification compares canonical identities and parsed payloads.

- [ ] Add a failing cross-EOL source-verification test.
- [ ] Replace private and raw JSON hash implementations with the shared semantic function.
- [ ] Update registry source identities only.
- [ ] Verify all active and archived baselines plus the active execution rule.

### Task 3: Repository LF contract and experiment reuse

**Files:**
- Modify: `.gitattributes`
- Modify: `src/czsc_trader/experiment_archive.py`
- Modify: `tests/test_cli_e2e.py`

**Interfaces:**
- Ordinary text: `text=auto eol=lf`.
- Raw market CSV: `-text`.
- Experiment text records consume `normalized_text_sha256`.

- [ ] Add a failing representative `git check-attr` test.
- [ ] Replace per-file CRLF rules with repository-wide LF and binary exceptions.
- [ ] Reuse shared normalized text identity in experiment manifests.
- [ ] Run `git add --renormalize .` and inspect the exact staged scope.

### Task 4: Documentation and portable verification

**Files:**
- Modify: `README.md`
- Modify: `docs/RESEARCH_HANDOFF.md`

- [ ] Document LF policy and identity categories.
- [ ] Run focused identity, baseline, execution, archive, and backtest tests.
- [ ] Run compile, all-archive validation, `git diff --check`, and attribute audit.
- [ ] Verify a clean independent checkout resolves active identities.
- [ ] Commit and report the branch without merging or pushing unless requested.
