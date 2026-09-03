# SE Statistical Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Strategy Evaluator own the complete engineering and statistical audit of an OPC-v3 provisional champion, while Trader only supplies immutable facts, executes requested stress scenarios, and persists results.

**Architecture:** The independent `strategy_evaluator` package gains NumPy/SciPy-backed audit modules and one `audit_provisional_champion` use-case. Trader converts existing Pandas/backtest objects to SE contracts, produces candidate return and execution-stress evidence, and stores SE outputs; all audit algorithms, evidence interpretation, risk labels, and final decision semantics remain inside SE.

**Tech Stack:** Python 3.12, frozen dataclasses, NumPy 2.4+, SciPy, existing CZSC Trader backtest primitives, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-03-se-statistical-audit-design.md`

## Global Constraints

- Develop on `codex/se-statistical-audit`; do not use a worktree or subagent.
- `strategy_evaluator` remains an independent package imported only by Trader.
- SE may depend on NumPy and SciPy; its public contracts may not expose Pandas or Trader types.
- Every provisional-champion audit rule, formula, validation rule, risk label, and decision belongs to SE.
- Trader may produce facts, run backtests requested by SE, cache evidence, and persist results; it may not decide audit status.
- `opc-v1`, `opc-v2`, and all existing experiment archives remain immutable.
- OPC-v3 statistical numbers are mandatory evidence but do not automatically veto a complete recommendation.
- No shadow account, automatic strategy switch, automatic freeze, or PTE registration is added.
- Fixed defaults are CSCV 10 blocks, bootstrap 10,000 replications, block lengths 21/10/42, and slippage 15/30/50bp.
- Each production change follows red-green-refactor and each task ends in a focused commit.

---

### Task 1: Add OPC-v3 audit contracts and standard

**Files:**
- Modify: `packages/strategy_evaluator/pyproject.toml`
- Create: `packages/strategy_evaluator/src/strategy_evaluator/audit_models.py`
- Modify: `packages/strategy_evaluator/src/strategy_evaluator/standards.py`
- Modify: `packages/strategy_evaluator/src/strategy_evaluator/__init__.py`
- Create: `packages/strategy_evaluator/tests/test_audit_models.py`
- Modify: `packages/strategy_evaluator/tests/test_validation.py`

**Interfaces:**
- Produces: `AuditStatus`, `RiskLabel`, `AuditIdentity`, `ReturnMatrixEvidence`, `ParameterPoint`, `ExecutionOrder`, `FactorEvent`, `ExecutionEvidence`, `StressScenarioResult`, `ChampionAuditRequest`, `AuditFinding`, and `ChampionAuditResult`.
- Produces: `OPC_V3 = EvaluationStandard("opc-v3", OPC_V2.margins)`.
- Consumes: existing `CandidateDescriptor`, `MetricObservation`, `TrialRecord`, `CandidateProfile`, and frozen record serialization.

- [ ] **Step 1: Write failing contract tests**

```python
def test_audit_request_is_immutable_and_round_trips():
    request = ChampionAuditRequest.from_dict(AUDIT_REQUEST)
    assert request.identity.standard_version == "opc-v3"
    assert ChampionAuditRequest.from_dict(request.to_dict()) == request
    with pytest.raises(FrozenInstanceError):
        request.champion_id = "changed"

def test_opc_v3_reuses_v2_deterministic_margins():
    assert OPC_V3.margins == OPC_V2.margins
    assert resolve_margins(protocol("opc-v3")) == OPC_V2.margins
```

- [ ] **Step 2: Verify RED**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/test_audit_models.py tests/test_validation.py -q` from `packages/strategy_evaluator`.

Expected: collection fails because the audit models and `OPC_V3` do not exist.

- [ ] **Step 3: Implement minimal immutable contracts**

Use exact public shapes:

```python
class AuditStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT = "INSUFFICIENT"

class RiskLabel(str, Enum):
    FAVORABLE = "FAVORABLE"
    MIXED = "MIXED"
    WEAK = "WEAK"

@dataclass(frozen=True)
class AuditIdentity(Record):
    experiment_id: str
    pool_hash: str
    data_cutoff: str
    data_hash: str
    execution_policy_hash: str
    standard_version: str
    audit_protocol_version: str
    seed: int

@dataclass(frozen=True)
class ReturnMatrixEvidence(Record):
    dates: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    returns: tuple[tuple[float, ...], ...]
    content_hash: str

@dataclass(frozen=True)
class ChampionAuditRequest(Record):
    identity: AuditIdentity
    champion_id: str
    incumbent_id: str
    pareto_peer_ids: tuple[str, ...]
    search_returns: ReturnMatrixEvidence
    comparison_returns: ReturnMatrixEvidence
    parameters: tuple[ParameterPoint, ...]
    execution: ExecutionEvidence
    formal_observations: tuple[MetricObservation, ...]
    repeated_observations: tuple[MetricObservation, ...]
    candidates: tuple[CandidateDescriptor, ...]
    trials: tuple[TrialRecord, ...]
    profiles: tuple[CandidateProfile, ...]
    stress_results: tuple[StressScenarioResult, ...]
```

Reject unknown fields and normalize lists to tuples. Validate SHA-256 strings in the audit use case, not constructors.

- [ ] **Step 4: Verify GREEN**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/test_audit_models.py tests/test_validation.py -q` from `packages/strategy_evaluator`.

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add packages/strategy_evaluator/pyproject.toml packages/strategy_evaluator/src/strategy_evaluator/audit_models.py packages/strategy_evaluator/src/strategy_evaluator/standards.py packages/strategy_evaluator/src/strategy_evaluator/__init__.py packages/strategy_evaluator/tests/test_audit_models.py packages/strategy_evaluator/tests/test_validation.py
git commit -m "feat: add OPC-v3 audit contracts"
```

### Task 2: Move search-bias statistics into SE

**Files:**
- Create: `packages/strategy_evaluator/src/strategy_evaluator/search_bias.py`
- Modify: `packages/strategy_evaluator/src/strategy_evaluator/__init__.py`
- Create: `packages/strategy_evaluator/tests/test_search_bias.py`
- Modify: `src/czsc_trader/robustness.py`
- Modify: `tests/test_robustness.py`

**Interfaces:**
- Produces: `annualized_sharpe(values) -> float`, `effective_trial_count(matrix) -> float`, `cscv_pbo(evidence, block_count=10) -> SearchBiasResult`, and `deflated_sharpe_ratio(selected, trial_sharpes, trial_count) -> DsrResult`.
- `SearchBiasResult` includes 252 split records, PBO, median validation percentile, selection frequencies, and OOS ranks.
- `DsrResult` includes raw and effective-trial DSR summaries.
- Trader `robustness.py` becomes a compatibility re-export for historical imports.

- [ ] **Step 1: Write failing statistical tests**

```python
def test_cscv_uses_all_252_complement_splits():
    result = cscv_pbo(matrix_evidence(), block_count=10)
    assert len(result.splits) == 252
    assert 0.0 <= result.pbo <= 1.0
    assert sum(count for _, count in result.selection_frequency) == 252

def test_effective_trials_collapse_for_correlated_candidates():
    correlated = np.column_stack([BASE, BASE, BASE])
    independent = np.column_stack([BASE, ALT1, ALT2])
    assert effective_trial_count(correlated) < effective_trial_count(independent)

def test_dsr_reports_raw_and_effective_trials():
    result = calculate_dsr_bundle(CHAMPION, TRIALS, raw_count=1187, effective_count=5.2)
    assert result.raw.trial_count == 1187
    assert result.effective.trial_count == pytest.approx(5.2)
```

- [ ] **Step 2: Verify RED**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/test_search_bias.py -q` from `packages/strategy_evaluator`.

Expected: collection fails because `search_bias` does not exist.

- [ ] **Step 3: Implement vectorized CSCV, effective dimension, and DSR**

Use 10 contiguous blocks and all five-block combinations. Use deterministic candidate-ID ordering for Sharpe ties. Compute effective trial count from finite eigenvalues of the candidate correlation matrix:

```python
effective = float(eigenvalues.sum() ** 2 / np.square(eigenvalues).sum())
```

Use the Bailey-Lopez de Prado expected-maximum-Sharpe expression for both raw and effective trial counts; accept a positive float trial count so the participation ratio is not rounded.

- [ ] **Step 4: Verify GREEN and compatibility**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/test_search_bias.py ..\..\tests/test_robustness.py -q` from `packages/strategy_evaluator`.

Expected: all selected tests pass and historic imports resolve through the shim.

- [ ] **Step 5: Commit**

```powershell
git add packages/strategy_evaluator/src/strategy_evaluator/search_bias.py packages/strategy_evaluator/src/strategy_evaluator/__init__.py packages/strategy_evaluator/tests/test_search_bias.py src/czsc_trader/robustness.py tests/test_robustness.py
git commit -m "feat: add SE search-bias audits"
```

### Task 3: Add paired stationary-block bootstrap

**Files:**
- Create: `packages/strategy_evaluator/src/strategy_evaluator/bootstrap.py`
- Modify: `packages/strategy_evaluator/src/strategy_evaluator/__init__.py`
- Create: `packages/strategy_evaluator/tests/test_bootstrap.py`

**Interfaces:**
- Produces: `performance_metrics(returns) -> PerformanceMetrics`.
- Produces: `paired_stationary_bootstrap(champion, comparator, *, repetitions, mean_block_length, seed) -> BootstrapComparison`.
- Produces: `audit_pairwise_bootstrap(request) -> tuple[BootstrapComparison, ...]` for incumbent and every declared Pareto peer at block lengths 21, 10, and 42.

- [ ] **Step 1: Write failing deterministic paired-bootstrap tests**

```python
def test_stationary_bootstrap_is_repeatable_and_paired():
    first = paired_stationary_bootstrap(CHAMPION, INCUMBENT, repetitions=500, mean_block_length=21, seed=7)
    second = paired_stationary_bootstrap(CHAMPION, INCUMBENT, repetitions=500, mean_block_length=21, seed=7)
    assert first == second
    assert first.cagr.probability_favorable > 0.5
    assert first.max_drawdown.direction == "higher_is_better"

def test_pairwise_audit_covers_incumbent_peers_and_three_lengths():
    rows = audit_pairwise_bootstrap(request(repetitions=100))
    assert {(row.comparator_id, row.mean_block_length) for row in rows} == {
        ("S001-v1", 21), ("S001-v1", 10), ("S001-v1", 42),
        ("R0539", 21), ("R0539", 10), ("R0539", 42),
    }
```

- [ ] **Step 2: Verify RED**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/test_bootstrap.py -q` from `packages/strategy_evaluator`.

Expected: collection fails because bootstrap functions do not exist.

- [ ] **Step 3: Implement stationary resampling and metric differences**

Generate one index path per repetition and apply it to both return series. At each position restart from a uniform random index with probability `1 / mean_block_length`, otherwise advance the prior index modulo sample length. Calculate CAGR, maximum drawdown, and Calmar from compounded wealth. Return point difference, 2.5%/97.5% quantiles, and favorable probability for each metric.

- [ ] **Step 4: Verify GREEN**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/test_bootstrap.py -q` from `packages/strategy_evaluator`.

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add packages/strategy_evaluator/src/strategy_evaluator/bootstrap.py packages/strategy_evaluator/src/strategy_evaluator/__init__.py packages/strategy_evaluator/tests/test_bootstrap.py
git commit -m "feat: add paired block bootstrap audit"
```

### Task 4: Add real parameter-neighborhood audit

**Files:**
- Create: `packages/strategy_evaluator/src/strategy_evaluator/neighborhood.py`
- Modify: `packages/strategy_evaluator/src/strategy_evaluator/__init__.py`
- Create: `packages/strategy_evaluator/tests/test_neighborhood.py`

**Interfaces:**
- Produces: `audit_parameter_neighborhood(champion_id, points, *, neighbor_limit=20, minimum_valid=10) -> NeighborhoodAudit`.
- Selects unique behavior hashes by ascending raw Range-weight L1 distance, then candidate ID.
- Returns selected neighbor rows plus median, Q1, worst, and degradation for each of the four worst-profile scores.

- [ ] **Step 1: Write failing nearest-neighbor tests**

```python
def test_neighborhood_uses_nearest_twenty_unique_behaviors():
    result = audit_parameter_neighborhood("R1102", POINTS, neighbor_limit=20, minimum_valid=10)
    assert len(result.neighbors) == 20
    assert len({row.behavior_hash for row in result.neighbors}) == 20
    assert tuple(row.distance for row in result.neighbors) == tuple(sorted(row.distance for row in result.neighbors))
    assert result.status is AuditStatus.PASS

def test_neighborhood_is_insufficient_below_ten_valid_neighbors():
    result = audit_parameter_neighborhood("R1102", POINTS[:9], minimum_valid=10)
    assert result.status is AuditStatus.INSUFFICIENT
```

- [ ] **Step 2: Verify RED**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/test_neighborhood.py -q` from `packages/strategy_evaluator`.

Expected: collection fails because the neighborhood audit does not exist.

- [ ] **Step 3: Implement behavior-deduplicated L1 neighborhood summaries**

Require identical ordered parameter names and finite values. Exclude the champion and incumbent. Keep the nearest representative per behavior hash. Compute Q1 with NumPy linear quantiles, then median, minimum, and champion-minus-neighborhood summary for `net_cagr`, `max_drawdown`, `calmar`, and `profit_factor` profile scores.

- [ ] **Step 4: Verify GREEN**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/test_neighborhood.py -q` from `packages/strategy_evaluator`.

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add packages/strategy_evaluator/src/strategy_evaluator/neighborhood.py packages/strategy_evaluator/src/strategy_evaluator/__init__.py packages/strategy_evaluator/tests/test_neighborhood.py
git commit -m "feat: add SE parameter neighborhood audit"
```

### Task 5: Move original champion engineering audits into SE

**Files:**
- Create: `packages/strategy_evaluator/src/strategy_evaluator/engineering_audit.py`
- Modify: `packages/strategy_evaluator/src/strategy_evaluator/__init__.py`
- Create: `packages/strategy_evaluator/tests/test_engineering_audit.py`
- Modify: `src/czsc_trader/audit.py`
- Modify: `tests/test_evaluation_audit.py`

**Interfaces:**
- Produces: `audit_execution(evidence) -> AuditFinding`.
- Produces: `audit_reproducibility(formal, repeated, tolerance=1e-12) -> AuditFinding`.
- Produces: `audit_trial_ledger(champion_id, candidates, trials, profiles) -> AuditFinding`.
- Produces: `required_stress_scenarios() -> tuple[StressScenario, ...]` and `audit_stress_results(...) -> StressAudit`.
- Trader `audit_candidate_evaluation` becomes an input-conversion compatibility wrapper that delegates its verdict to SE.

- [ ] **Step 1: Write failing engineering-audit tests**

```python
def test_execution_audit_rejects_non_next_day_fill():
    evidence = execution_evidence(execution_date="2026-01-09")
    assert audit_execution(evidence).status is AuditStatus.FAIL

def test_reproducibility_checks_values_status_and_windows():
    assert audit_reproducibility(FORMAL, FORMAL).status is AuditStatus.PASS
    changed = replace(FORMAL[0], net_cagr=FORMAL[0].net_cagr + 1e-6)
    assert audit_reproducibility(FORMAL, (changed, *FORMAL[1:])).status is AuditStatus.FAIL

def test_stress_requires_fee_and_all_slippage_scenarios():
    assert {row.scenario_id for row in required_stress_scenarios()} == {
        "fee_x2", "slippage_15bp", "slippage_30bp", "slippage_50bp"
    }
    assert audit_stress_results(COMPLETE_STRESS).status is AuditStatus.PASS
    assert audit_stress_results(COMPLETE_STRESS[:-1]).status is AuditStatus.INSUFFICIENT
```

- [ ] **Step 2: Verify RED**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/test_engineering_audit.py -q` from `packages/strategy_evaluator`.

Expected: collection fails because the engineering audit module does not exist.

- [ ] **Step 3: Implement exact original checks and stress interpretation**

Port candidate execution semantics from `czsc_trader.audit.audit_candidate_evaluation` using only frozen SE records. Compare formal observations by `(candidate_id, window_id, scenario_id, measurement_tier)`. Validate trial-to-candidate hashes and champion ranked eligibility. For each stress scenario compute champion-minus-incumbent metric differences and advantage shrinkage from the standard observations.

- [ ] **Step 4: Verify GREEN and wrapper compatibility**

Run: `.\.venv\Scripts\python.exe -m pytest packages/strategy_evaluator/tests/test_engineering_audit.py tests/test_evaluation_audit.py tests/test_candidate_evaluation.py -q` from repository root.

Expected: all selected tests pass; existing Trader callers now receive an SE-derived verdict.

- [ ] **Step 5: Commit**

```powershell
git add packages/strategy_evaluator/src/strategy_evaluator/engineering_audit.py packages/strategy_evaluator/src/strategy_evaluator/__init__.py packages/strategy_evaluator/tests/test_engineering_audit.py src/czsc_trader/audit.py tests/test_evaluation_audit.py
git commit -m "refactor: move champion engineering audits to SE"
```

### Task 6: Add the unified SE audit use case and OPC-v3 finalization

**Files:**
- Create: `packages/strategy_evaluator/src/strategy_evaluator/champion_audit.py`
- Modify: `packages/strategy_evaluator/src/strategy_evaluator/evaluator.py`
- Modify: `packages/strategy_evaluator/src/strategy_evaluator/reporting.py`
- Modify: `packages/strategy_evaluator/src/strategy_evaluator/__init__.py`
- Create: `packages/strategy_evaluator/tests/test_champion_audit.py`
- Modify: `packages/strategy_evaluator/tests/test_evaluator.py`
- Modify: `packages/strategy_evaluator/tests/test_reporting.py`

**Interfaces:**
- Produces: `audit_provisional_champion(request) -> ChampionAuditResult`.
- Produces: `finalize_evaluation(..., audit: ChampionAuditResult | None = None, standard_version: str = "opc-v1")`.
- Produces: report sections for statistical evidence, engineering evidence, risk label, and human-decision boundary.

- [ ] **Step 1: Write failing orchestration and decision tests**

```python
def test_complete_opc_v3_audit_can_recommend_even_when_statistical_label_is_weak():
    audit = audit_provisional_champion(complete_request(statistical_pattern="weak"))
    result = finalize_evaluation(RANKING, None, "EX", audit=audit, standard_version="opc-v3")
    assert audit.risk_label is RiskLabel.WEAK
    assert result.decision is Decision.RECOMMEND_FREEZE

def test_missing_opc_v3_audit_is_insufficient():
    result = finalize_evaluation(RANKING, None, "EX", standard_version="opc-v3")
    assert result.decision is Decision.INSUFFICIENT_EVIDENCE

def test_engineering_failure_keeps_incumbent():
    audit = audit_provisional_champion(complete_request(execution_invalid=True))
    result = finalize_evaluation(RANKING, None, "EX", audit=audit, standard_version="opc-v3")
    assert result.decision is Decision.KEEP_INCUMBENT
```

- [ ] **Step 2: Verify RED**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/test_champion_audit.py tests/test_evaluator.py tests/test_reporting.py -q` from `packages/strategy_evaluator`.

Expected: tests fail because the unified audit and OPC-v3 finalization path do not exist.

- [ ] **Step 3: Implement validation, orchestration, labels, and report data**

Validate every identity and content hash before calculation. Run engineering audits, PBO/DSR, paired bootstrap, and neighborhood exactly once. Set overall status to `FAIL` for engineering contradiction, `INSUFFICIENT` for missing/invalid mandatory evidence, otherwise `PASS`. Assign the statistical label deterministically from primary PBO/DSR, incumbent-bootstrap directions, sensitivity consistency, and neighborhood summaries; store every contributing direction flag in the result.

- [ ] **Step 4: Verify GREEN and v1/v2 compatibility**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/test_champion_audit.py tests/test_evaluator.py tests/test_reporting.py -q` from `packages/strategy_evaluator`.

Expected: all selected tests pass, including unchanged v1/v2 health finalization.

- [ ] **Step 5: Commit**

```powershell
git add packages/strategy_evaluator/src/strategy_evaluator/champion_audit.py packages/strategy_evaluator/src/strategy_evaluator/evaluator.py packages/strategy_evaluator/src/strategy_evaluator/reporting.py packages/strategy_evaluator/src/strategy_evaluator/__init__.py packages/strategy_evaluator/tests/test_champion_audit.py packages/strategy_evaluator/tests/test_evaluator.py packages/strategy_evaluator/tests/test_reporting.py
git commit -m "feat: unify provisional champion audit in SE"
```

### Task 7: Adapt Trader to produce evidence and persist OPC-v3 artifacts

**Files:**
- Create: `src/czsc_trader/application/evaluation_evidence.py`
- Modify: `src/czsc_trader/application/evaluation_service.py`
- Modify: `src/czsc_trader/candidate_evaluation.py`
- Modify: `src/czsc_trader/evaluation_artifacts.py`
- Create: `tests/test_evaluation_evidence.py`
- Modify: `tests/test_evaluation_service.py`
- Modify: `tests/test_evaluation_artifacts.py`
- Modify: `tests/test_strategy_cli.py`

**Interfaces:**
- Produces: `build_champion_audit_request(...) -> ChampionAuditRequest`.
- Produces: cached candidate-return evidence keyed by pool/data/cutoff/execution/audit protocol/seed.
- Produces: stress scenario execution using `required_stress_scenarios()`.
- Persists: `statistical_audit.json`, `cscv_splits.csv`, `bootstrap_comparisons.csv`, `parameter_neighborhood.csv`, and `execution_stress.csv`.

- [ ] **Step 1: Write failing Trader integration tests**

```python
def test_opc_v3_service_delegates_complete_audit_to_se(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(evaluation_service, "audit_provisional_champion", lambda request: seen.append(request) or COMPLETE_AUDIT)
    result = evaluate_experiment(context(tmp_path), "EX_V3", runner=fake_runner)
    assert len(seen) == 1
    assert result.payload["standard_version"] == "opc-v3"
    assert (tmp_path / "experiments/EX_V3/artifacts/statistical_audit.json").is_file()

def test_accept_rejects_opc_v3_result_without_complete_audit(tmp_path):
    write_result(tmp_path, decision="RECOMMEND_FREEZE", audit_status="INSUFFICIENT")
    with pytest.raises(ValueError, match="complete statistical audit"):
        accept_evaluation(context(tmp_path), "EX_V3", "actor", "reason")
```

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_evaluation_evidence.py tests/test_evaluation_service.py tests/test_evaluation_artifacts.py tests/test_strategy_cli.py -q` from repository root.

Expected: tests fail because OPC-v3 evidence construction and artifacts are absent.

- [ ] **Step 3: Implement Trader-only evidence production and persistence**

Remove `_health` from `evaluation_service.py`. For v1/v2 retain the existing `HealthEvidence` flow through a compatibility helper owned by SE; for v3 build normalized evidence, request all SE stress scenarios, call `audit_provisional_champion`, and pass the result to finalization. Serialize the SE result and detail rows without recomputing any audit verdict in Trader. Extend completed-result hash validation to every OPC-v3 audit artifact.

- [ ] **Step 4: Verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_evaluation_evidence.py tests/test_evaluation_service.py tests/test_evaluation_artifacts.py tests/test_strategy_cli.py -q` from repository root.

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/czsc_trader/application/evaluation_evidence.py src/czsc_trader/application/evaluation_service.py src/czsc_trader/candidate_evaluation.py src/czsc_trader/evaluation_artifacts.py tests/test_evaluation_evidence.py tests/test_evaluation_service.py tests/test_evaluation_artifacts.py tests/test_strategy_cli.py
git commit -m "feat: integrate OPC-v3 audit evidence"
```

### Task 8: Verify performance, document migration, and prepare EX06

**Files:**
- Modify: `packages/strategy_evaluator/README.md`
- Modify: `README.md`
- Modify: `docs/RESEARCH_HANDOFF.md`
- Modify: `docs/DEVELOPMENT_HANDOFF.md`
- Create: `experiments/0903_EX06/01_goal.md`
- Create: `experiments/0903_EX06/02_design.md`
- Create: `experiments/0903_EX06/evaluation_protocol.json`
- Create: `experiments/0903_EX06/experiment_manifest.json`
- Create: `experiments/0903_EX06/run_experiment.py`
- Create: `tests/test_opc_v3_statistical_audit.py`

**Interfaces:**
- EX06 reuses EX05's frozen candidate pool and cutoff but writes a new OPC-v3 archive.
- The benchmark records cold end-to-end and warm statistical-only elapsed time in the EX06 archive.

- [ ] **Step 1: Write the failing EX06 contract test**

```python
def test_ex06_reuses_ex05_pool_under_opc_v3_without_mutating_ex05():
    protocol = json.loads(Path("experiments/0903_EX06/evaluation_protocol.json").read_text())
    manifest = json.loads(Path("experiments/0903_EX06/experiment_manifest.json").read_text())
    assert protocol["standard_version"] == "opc-v3"
    assert manifest["reuse_source_experiments"] == ["0903_EX05"]
    assert manifest["audit_protocol"]["bootstrap_repetitions"] == 10_000
    assert manifest["audit_protocol"]["mean_block_lengths"] == [21, 10, 42]
```

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_opc_v3_statistical_audit.py -q` from repository root.

Expected: test fails because EX06 does not exist.

- [ ] **Step 3: Add EX06 preregistration and operator documentation**

Copy the immutable candidate manifest by reference/reuse identity, never by editing EX05. State that R0539 is a statistical comparator only, no shadow account is created, and EX06 completion requires human freeze confirmation. Document the single `strategy evaluate` command and the meaning of `RECOMMEND_FREEZE + WEAK`.

- [ ] **Step 4: Run focused and full verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest packages/strategy_evaluator/tests tests -q
.\.venv\Scripts\python.exe -m ruff check packages/strategy_evaluator/src packages/strategy_evaluator/tests src/czsc_trader/application/evaluation_service.py src/czsc_trader/application/evaluation_evidence.py tests/test_evaluation_evidence.py tests/test_opc_v3_statistical_audit.py
```

Expected: all tests pass and Ruff exits zero.

- [ ] **Step 5: Run EX06 and record performance**

Run:

```powershell
.\.venv\Scripts\python.exe experiments\0903_EX06\run_experiment.py
```

Expected: the archive contains all OPC-v3 artifacts, its identity hashes validate, end-to-end runtime is at most 300 seconds, statistical-only runtime is at most 120 seconds, and no SM/PTE state changes occur.

- [ ] **Step 6: Commit**

```powershell
git add packages/strategy_evaluator/README.md README.md docs/RESEARCH_HANDOFF.md docs/DEVELOPMENT_HANDOFF.md experiments/0903_EX06 tests/test_opc_v3_statistical_audit.py
git commit -m "research: evaluate range finalists with OPC-v3"
```
