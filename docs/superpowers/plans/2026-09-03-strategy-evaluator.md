# Strategy Evaluator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an OPC-sized strategy evaluation package and Trader workflow that can reject materially inferior candidates, rank robust challengers, and recommend at most one human-approved paper-trading release.

**Architecture:** A standard-library-only `strategy_evaluator` package owns immutable contracts, OPC-v1 margins, non-inferiority, Pareto ranking, finalization, and summary rendering. CZSC Trader remains the orchestration boundary: it loads experiment bundles, invokes existing market/backtest primitives, writes artifacts, freezes an accepted winner through Strategy Manager, and invokes the existing PTE account CLI without creating package-level dependency cycles.

**Tech Stack:** Python 3.12, dataclasses, enum, hashlib/json/csv, argparse, existing CZSC Trader backtest primitives, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-03-strategy-evaluator-design.md`

## Global Constraints

- Develop on `codex/strategy-evaluator`; do not use a worktree.
- `strategy_evaluator` is an independent package under `packages/` and has no runtime dependencies.
- Production dependency direction is only `czsc_trader -> strategy_evaluator`.
- SE never reads repository paths, runs backtests, writes files, changes SM state, or imports PTE.
- OPC-v1 decision metrics are net CAGR, maximum drawdown, Calmar, and Profit Factor.
- Experiment margins may tighten OPC-v1 defaults and may never loosen them.
- Historical experiment archives remain byte-for-byte unchanged.
- Evaluation never freezes automatically; acceptance is a separate explicit human action.
- Preserve the unrelated working-tree change in `configs/backtest_windows/2026.json` and never stage it.
- Each implementation task follows red-green-refactor and ends with a focused commit.

---

### Task 1: Scaffold the independent package and immutable contracts

**Files:**
- Create: `packages/strategy_evaluator/pyproject.toml`
- Create: `packages/strategy_evaluator/README.md`
- Create: `packages/strategy_evaluator/src/strategy_evaluator/__init__.py`
- Create: `packages/strategy_evaluator/src/strategy_evaluator/models.py`
- Create: `packages/strategy_evaluator/tests/test_models.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `Decision`, `MetricStatus`, `HealthStatus`, `CandidateDescriptor`, `MetricObservation`, `TrialRecord`, `EvaluationProtocol`, `HealthEvidence`, `ShortlistResult`, `CandidateProfile`, `RankingResult`, and `EvaluationResult`.
- Produces: every public record has `to_dict()`; top-level input records have `from_dict()`.
- Consumes: no project package.

- [ ] **Step 1: Write failing model and dependency tests**

```python
def test_protocol_and_observation_are_immutable_and_json_compatible():
    protocol = EvaluationProtocol.from_dict(PROTOCOL)
    observation = MetricObservation.from_dict(OBSERVATION)
    assert protocol.standard_version == "opc-v1"
    assert observation.profit_factor_status is MetricStatus.VALID
    assert EvaluationProtocol.from_dict(protocol.to_dict()) == protocol
    with pytest.raises(FrozenInstanceError):
        protocol.experiment_id = "changed"

def test_decision_values_are_closed():
    assert {item.value for item in Decision} == {
        "RECOMMEND_FREEZE", "KEEP_INCUMBENT", "INSUFFICIENT_EVIDENCE"
    }
```

- [ ] **Step 2: Run the package tests and verify RED**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/test_models.py -q` from `packages/strategy_evaluator`.

Expected: collection fails because `strategy_evaluator` does not exist.

- [ ] **Step 3: Add package metadata and immutable models**

Use a package layout matching `packages/strategy_manager`. Implement enum conversion explicitly and reject unknown or missing fields. Store sequences as tuples and mapping fields as sorted tuples of `(name, value)` pairs so frozen records contain no mutable containers.

```python
class Decision(str, Enum):
    RECOMMEND_FREEZE = "RECOMMEND_FREEZE"
    KEEP_INCUMBENT = "KEEP_INCUMBENT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"

class MetricStatus(str, Enum):
    VALID = "VALID"
    LOW_SAMPLE = "LOW_SAMPLE"
    NO_CLOSED_TRADES = "NO_CLOSED_TRADES"
    NO_WINS = "NO_WINS"
    NO_LOSSES = "NO_LOSSES"
    UNAVAILABLE = "UNAVAILABLE"

@dataclass(frozen=True)
class MetricObservation:
    candidate_id: str
    window_id: str
    scenario_id: str
    measurement_tier: str
    net_cagr: float
    total_return: float
    max_drawdown: float
    calmar: float | None
    calmar_status: MetricStatus
    profit_factor: float | None
    profit_factor_status: MetricStatus
    closed_trades: int
    turnover: float | None = None
    cost_drag: float | None = None
    objective_values: tuple[tuple[str, float], ...] = ()
```

Add `czsc-strategy-evaluator==0.1.0` to the root project dependencies. Install editable with:

```powershell
.\.venv\Scripts\python.exe -m pip install -e packages\strategy_evaluator -e .
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest packages\strategy_evaluator\tests\test_models.py -q`

Expected: all model tests pass.

- [ ] **Step 5: Commit the package foundation**

```powershell
git add pyproject.toml packages\strategy_evaluator
git commit -m "feat: define strategy evaluation contracts"
```

### Task 2: Implement OPC-v1 standards and protocol validation

**Files:**
- Create: `packages/strategy_evaluator/src/strategy_evaluator/standards.py`
- Create: `packages/strategy_evaluator/src/strategy_evaluator/validation.py`
- Create: `packages/strategy_evaluator/tests/test_validation.py`
- Modify: `packages/strategy_evaluator/src/strategy_evaluator/__init__.py`

**Interfaces:**
- Consumes: Task 1 models.
- Produces: `OPC_V1`, `MarginSet`, `validate_protocol(protocol, candidates, observations, trials)`.
- Produces: `ValidationError` with stable `code` and human-readable message.

- [ ] **Step 1: Write failing validation tests**

```python
def test_protocol_can_tighten_but_cannot_loosen_defaults():
    strict = protocol(tightened_margins={"net_cagr_retention": 0.95})
    validate_protocol(strict, CANDIDATES, OBSERVATIONS, TRIALS)
    loose = protocol(tightened_margins={"net_cagr_retention": 0.80})
    with pytest.raises(ValidationError, match="cannot loosen"):
        validate_protocol(loose, CANDIDATES, OBSERVATIONS, TRIALS)

def test_validation_requires_one_incumbent_and_complete_trial_ledger():
    with pytest.raises(ValidationError, match="exactly one incumbent"):
        validate_protocol(PROTOCOL, NO_INCUMBENT, OBSERVATIONS, TRIALS)
    with pytest.raises(ValidationError, match="trial ledger"):
        validate_protocol(PROTOCOL, CANDIDATES, OBSERVATIONS, TRIALS[:-1])
```

- [ ] **Step 2: Run validation tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest packages\strategy_evaluator\tests\test_validation.py -q`

Expected: imports fail for `standards` and `validation`.

- [ ] **Step 3: Implement defaults and validation**

```python
@dataclass(frozen=True)
class MarginSet:
    net_cagr_retention: float = 0.90
    max_drawdown_absolute: float = 0.02
    max_drawdown_relative: float = 0.15
    calmar_retention: float = 0.90
    profit_factor_retention: float = 0.85
    profit_factor_floor: float = 1.0
    minimum_closed_trades: int = 10

OPC_V1 = EvaluationStandard(version="opc-v1", margins=MarginSet())
```

Validate finite metric values, non-positive drawdown, status/value consistency, supported tiers, unique IDs, one incumbent, candidate/trial coverage, full incumbent window coverage, matching execution hash, and target requirement names. Return no partially valid protocol.

- [ ] **Step 4: Run validation and model tests**

Run: `.\.venv\Scripts\python.exe -m pytest packages\strategy_evaluator\tests\test_models.py packages\strategy_evaluator\tests\test_validation.py -q`

Expected: all pass.

- [ ] **Step 5: Commit OPC-v1 standards**

```powershell
git add packages\strategy_evaluator
git commit -m "feat: validate OPC strategy evaluation protocols"
```

### Task 3: Implement non-inferiority, worst-window profiles, and Pareto ranking

**Files:**
- Create: `packages/strategy_evaluator/src/strategy_evaluator/noninferiority.py`
- Create: `packages/strategy_evaluator/src/strategy_evaluator/pareto.py`
- Create: `packages/strategy_evaluator/src/strategy_evaluator/evaluator.py`
- Create: `packages/strategy_evaluator/tests/test_noninferiority.py`
- Create: `packages/strategy_evaluator/tests/test_pareto.py`
- Create: `packages/strategy_evaluator/tests/test_evaluator.py`
- Modify: `packages/strategy_evaluator/src/strategy_evaluator/__init__.py`

**Interfaces:**
- Consumes: validated models and `MarginSet`.
- Produces: `compare_observation(candidate, incumbent, margins) -> tuple[MetricComparison, ...]`.
- Produces: `pareto_layers(profiles) -> tuple[CandidateProfile, ...]`.
- Produces: `screen_candidates(...) -> ShortlistResult` and `rank_candidates(...) -> RankingResult`.

- [ ] **Step 1: Write RED tests for the exact default boundaries**

```python
def test_candidate_fails_when_positive_cagr_retention_is_below_ninety_percent():
    comparisons = compare_observation(obs(net_cagr=.089), obs(net_cagr=.10), MarginSet())
    assert comparison(comparisons, "net_cagr").passed is False

def test_drawdown_uses_the_stricter_absolute_and_relative_boundary():
    comparisons = compare_observation(
        obs(max_drawdown=-.116), obs(max_drawdown=-.10), MarginSet()
    )
    assert comparison(comparisons, "max_drawdown").passed is False

def test_low_sample_profit_factor_cannot_prove_superiority():
    result = compare_observation(
        obs(profit_factor=9, closed_trades=4, profit_factor_status="LOW_SAMPLE"),
        obs(profit_factor=2, closed_trades=20),
        MarginSet(),
    )
    assert comparison(result, "profit_factor").comparable is False
```

- [ ] **Step 2: Verify non-inferiority tests fail**

Run: `.\.venv\Scripts\python.exe -m pytest packages\strategy_evaluator\tests\test_noninferiority.py -q`

Expected: `compare_observation` is unavailable.

- [ ] **Step 3: Implement metric-specific boundaries and normalized scores**

Implement favorable differences with explicit near-zero branches. A passing boundary has score at least
`-1.0`; equal performance is `0.0`. Profit Factor is compared only when both sides are valid and the
candidate has at least ten full-period or target-window closed trades.

- [ ] **Step 4: Verify non-inferiority tests pass**

Run: `.\.venv\Scripts\python.exe -m pytest packages\strategy_evaluator\tests\test_noninferiority.py -q`

Expected: all pass.

- [ ] **Step 5: Write RED tests for worst-window and Pareto behavior**

```python
def test_one_bad_ytd_window_eliminates_a_strong_long_term_candidate():
    ranking = rank_candidates(PROTOCOL, SHORTLIST, formal_metrics_with_bad_ytd())
    rejected = next(item for item in ranking.profiles if item.candidate_id == "1010")
    assert rejected.eligible is False
    assert "NONINFERIORITY_NET_CAGR_2026_YTD" in rejected.reason_codes

def test_pareto_keeps_tradeoffs_and_does_not_force_a_winner():
    ranked = pareto_layers(TRADEOFF_PROFILES)
    assert {item.candidate_id for item in ranked if item.pareto_layer == 1} == {"a", "b"}
```

- [ ] **Step 6: Implement profiles, deterministic fronts, screening, and ranking**

For each metric take the minimum applicable normalized score across decision windows. Pareto maximizes
the four worst scores. Sort by eligibility, target achievement, Pareto layer, median score, turnover,
parameter distance, and candidate ID. Preserve the full first front even when it exceeds the soft
shortlist target.

- [ ] **Step 7: Run all ranking tests**

Run: `.\.venv\Scripts\python.exe -m pytest packages\strategy_evaluator\tests -q`

Expected: all package tests pass.

- [ ] **Step 8: Commit the evaluation engine**

```powershell
git add packages\strategy_evaluator
git commit -m "feat: rank strategy challengers by worst-window evidence"
```

### Task 4: Finalize health decisions and render the one-page report

**Files:**
- Create: `packages/strategy_evaluator/src/strategy_evaluator/reporting.py`
- Create: `packages/strategy_evaluator/tests/test_reporting.py`
- Modify: `packages/strategy_evaluator/src/strategy_evaluator/evaluator.py`
- Modify: `packages/strategy_evaluator/tests/test_evaluator.py`
- Modify: `packages/strategy_evaluator/src/strategy_evaluator/__init__.py`

**Interfaces:**
- Consumes: `RankingResult` and `HealthEvidence`.
- Produces: `finalize_evaluation(ranking, health) -> EvaluationResult`.
- Produces: `render_summary(result) -> str`.

- [ ] **Step 1: Write RED tests for the three decisions**

```python
def test_only_a_unique_healthy_champion_recommends_freeze():
    result = finalize_evaluation(UNIQUE_WINNER, ALL_HEALTH_PASS)
    assert result.decision is Decision.RECOMMEND_FREEZE

def test_failed_health_keeps_incumbent_and_missing_health_is_insufficient():
    assert finalize_evaluation(UNIQUE_WINNER, FAILED_HEALTH).decision is Decision.KEEP_INCUMBENT
    assert finalize_evaluation(UNIQUE_WINNER, MISSING_HEALTH).decision is Decision.INSUFFICIENT_EVIDENCE
```

- [ ] **Step 2: Run finalization tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest packages\strategy_evaluator\tests\test_evaluator.py -q`

Expected: `finalize_evaluation` is unavailable.

- [ ] **Step 3: Implement stable reason codes and finalization**

Return `KEEP_INCUMBENT` when no challenger passes, no challenger meets the objective, or a health check
fails. Return `INSUFFICIENT_EVIDENCE` for ties and critical missing evidence. Never return a recommended
candidate with a non-freeze decision.

- [ ] **Step 4: Write and verify the report golden test**

```python
def test_report_is_one_page_and_contains_only_decision_fields():
    report = render_summary(RECOMMEND_RESULT)
    assert "建议冻结" in report
    assert "四项核心指标" in report
    assert report.count("支持理由") == 1
    assert len(report.splitlines()) <= 80
```

Run: `.\.venv\Scripts\python.exe -m pytest packages\strategy_evaluator\tests -q`

Expected: all package tests pass.

- [ ] **Step 5: Commit finalization and reporting**

```powershell
git add packages\strategy_evaluator
git commit -m "feat: finalize and report strategy evaluations"
```

### Task 5: Add Trader experiment-bundle orchestration and CLI

**Files:**
- Create: `src/czsc_trader/candidate_evaluation.py`
- Create: `src/czsc_trader/application/evaluation_service.py`
- Create: `tests/test_candidate_evaluation.py`
- Create: `tests/test_evaluation_service.py`
- Create: `tests/fixtures/strategy_evaluation/0903_TEST/evaluation_protocol.json`
- Create: `tests/fixtures/strategy_evaluation/0903_TEST/candidate_manifest.json`
- Modify: `src/czsc_trader/cli/strategy_commands.py`
- Modify: `tests/test_strategy_cli.py`

**Interfaces:**
- Consumes: SE public interfaces, a protocol, and a candidate manifest containing complete executable strategy payloads.
- Produces: `evaluate_candidate_payloads(context, protocol, candidates, candidate_ids, tier, scenarios=("standard",)) -> tuple[MetricObservation, ...]`.
- Produces: `evaluate_experiment(context, experiment_id) -> CommandResult`.
- Produces: `czsc-trader strategy evaluate --experiment ID`.
- Writes: evaluation result files only below the selected experiment.

- [ ] **Step 1: Write a RED application-service fixture test**

```python
def test_evaluate_experiment_writes_complete_atomic_result(tmp_path):
    context = fixture_repository(tmp_path, "0903_TEST")
    result = evaluate_experiment(context, "0903_TEST", runner=fake_candidate_runner)
    assert result.result["decision"] == "KEEP_INCUMBENT"
    artifact = context.experiments_root / "0903_TEST" / "artifacts"
    assert (artifact / "evaluation_result.json").is_file()
    assert (artifact / "evaluation_report.md").is_file()
```

- [ ] **Step 2: Verify service test fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_evaluation_service.py -q`

Expected: `evaluation_service` does not exist.

- [ ] **Step 3: Write RED tests for shared candidate backtesting**

```python
def test_candidate_runner_reuses_market_context_and_returns_standard_observations(monkeypatch):
    calls = install_tiny_market_stubs(monkeypatch)
    observations = evaluate_candidate_payloads(
        CONTEXT, PROTOCOL, TWO_COMPLETE_PAYLOADS, ("incumbent", "challenger"), "SCREENING"
    )
    assert calls["load_market_data"] == 1
    assert {item.candidate_id for item in observations} == {"incumbent", "challenger"}
    assert all(item.measurement_tier == "SCREENING" for item in observations)

def test_formal_tier_uses_embedded_execution_policy(monkeypatch):
    observations = evaluate_candidate_payloads(
        CONTEXT, PROTOCOL, TWO_COMPLETE_PAYLOADS, ("challenger",), "FORMAL"
    )
    assert observations[0].scenario_id == "standard"
    assert execution_spy.limit_policy_calls == 1
```

- [ ] **Step 4: Implement the shared candidate runner**

Load market data and generate the factor frame once. Resolve each complete candidate payload through
`resolve_strategy_payload`, apply it with `apply_resolved_baseline`, and calculate screening metrics with
`run_period_backtests`. For `FORMAL` and `STRESS`, reuse `simulate_limit_policy` and
`strategy_comparison_metrics` so the metrics match ordinary fixed-baseline execution semantics. Return
records only; never create an ordinary backtest output directory per candidate.

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_candidate_evaluation.py -q`

Expected: all runner tests pass.

- [ ] **Step 5: Implement safe bundle loading, orchestration, and atomic outputs**

Resolve the experiment as one direct child of `context.experiments_root`; reject traversal. Read the
protocol and complete candidate manifest. Run `SCREENING` for all behavior representatives, call
`screen_candidates`, run `FORMAL` for the deterministic shortlist, call `rank_candidates`, run only the
pre-recommended champion's pressure scenarios and reproducibility check, then call
`finalize_evaluation`. Write to a temporary sibling directory before replacing the seven evaluation
artifacts. Refuse to overwrite an existing `evaluation_result.json` with a different input hash.

The application service accepts an injected runner only for deterministic unit tests. Production always
uses `evaluate_candidate_payloads`. Candidate generation remains experiment-specific; candidate
evaluation and ranking are unified.

- [ ] **Step 6: Add and test the CLI action**

Add `strategy evaluate --experiment ID` to `strategy_commands.py` and dispatch to
`evaluate_experiment`.

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_evaluation_service.py tests\test_strategy_cli.py -q
.\.venv\Scripts\czsc-trader.exe strategy evaluate --help
```

Expected: tests pass and help shows required `--experiment`.

- [ ] **Step 7: Enforce dependency boundaries**

Add a test that scans production imports and asserts only files below `src/czsc_trader` import
`strategy_evaluator`; assert no source below `packages/strategy_manager` or
`packages/paper_trading_engine` contains that import.

- [ ] **Step 8: Commit Trader evaluation integration**

```powershell
git add src\czsc_trader\candidate_evaluation.py src\czsc_trader\application\evaluation_service.py src\czsc_trader\cli\strategy_commands.py tests
git commit -m "feat: evaluate strategy experiments through Trader"
```

### Task 6: Add explicit idempotent acceptance and PTE registration

**Files:**
- Modify: `src/czsc_trader/application/evaluation_service.py`
- Modify: `src/czsc_trader/cli/strategy_commands.py`
- Modify: `tests/test_evaluation_service.py`
- Modify: `tests/test_strategy_cli.py`

**Interfaces:**
- Consumes: an immutable `RECOMMEND_FREEZE` result and winning candidate payload in the experiment.
- Produces: `accept_evaluation(context, experiment_id, actor, reason) -> CommandResult`.
- Produces: `strategy accept-evaluation --experiment --actor --reason`.
- Invokes: existing `.venv/Scripts/pte.exe account create` as a subprocess; does not import PTE.

- [ ] **Step 1: Write RED tests for authorization, idempotency, and recovery**

```python
def test_accept_rejects_non_freeze_evaluation(tmp_path):
    with pytest.raises(ValidationError, match="RECOMMEND_FREEZE"):
        accept_evaluation(context_for("KEEP_INCUMBENT"), "EX", "tomxiao", "reviewed")

def test_accept_freezes_once_and_retries_only_pte_registration(tmp_path, fake_pte):
    first = accept_evaluation(ready_context(tmp_path), "EX", "tomxiao", "reviewed")
    second = accept_evaluation(ready_context(tmp_path), "EX", "tomxiao", "reviewed")
    assert first.result["release_id"] == second.result["release_id"]
    assert strategy_version_count(tmp_path) == 1
    assert fake_pte.create_calls == 2
```

- [ ] **Step 2: Verify acceptance tests fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_evaluation_service.py -q`

Expected: `accept_evaluation` is unavailable.

- [ ] **Step 3: Implement the acceptance journal and SM calls**

Store `evaluation_acceptance.json` in the experiment with input hash, release ID, SM state, PTE state,
actor, reason, and timestamps. Build the next StrategyVersion from the winning complete strategy payload,
add `RESEARCH_BACKTEST` evidence referencing `evaluation_result.json`, and use existing SM registry methods
to freeze it. On repeated calls, verify hashes and reuse the recorded release.

- [ ] **Step 4: Invoke the existing PTE account command safely**

Use an argument list without a shell:

```python
command = [
    str(context.root / ".venv" / "Scripts" / "pte.exe"),
    "account", "create", "--repo-root", str(context.root),
    "--account-id", release_id.lower(), "--name", strategy_name,
    "--strategy", strategy_id, "--strategy-version", version,
    "--symbol", symbol, "--initial-cash", "100000",
]
```

If PTE fails, persist `PAPER_ACTIVATION_PENDING` and return an actionable warning. Repeated acceptance
retries only this command. A successful account response persists `PAPER_ACTIVE`.

- [ ] **Step 5: Add the CLI and run integration tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_evaluation_service.py tests\test_strategy_cli.py packages\strategy_manager\tests packages\paper_trading_engine\tests\test_virtual_accounts.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit acceptance orchestration**

```powershell
git add src\czsc_trader\application\evaluation_service.py src\czsc_trader\cli\strategy_commands.py tests
git commit -m "feat: accept evaluated champions into paper trading"
```

### Task 7: Document operations and verify the complete repository

**Files:**
- Modify: `README.md`
- Modify: `docs/RESEARCH_HANDOFF.md`
- Modify: `docs/DEVELOPMENT_HANDOFF.md`
- Test: all root and package test suites.

**Interfaces:**
- Consumes: completed CLI and package behavior.
- Produces: cross-machine installation, evaluation, interpretation, and recovery instructions.

- [ ] **Step 1: Update user and handoff documentation**

Document editable installation of `packages/strategy_evaluator`, the three decisions, four metrics,
required experiment bundle, `strategy evaluate`, `strategy accept-evaluation`, 100,000 initial paper
capital, pending PTE recovery, and the rule that historical experiment archives are not rewritten.

- [ ] **Step 2: Run focused smoke commands**

```powershell
.\.venv\Scripts\czsc-trader.exe strategy evaluate --help
.\.venv\Scripts\czsc-trader.exe strategy accept-evaluation --help
.\.venv\Scripts\python.exe -c "import strategy_evaluator; print(strategy_evaluator.__version__)"
```

Expected: both help commands succeed and version prints `0.1.0`.

- [ ] **Step 3: Run complete verification**

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m pytest packages\strategy_evaluator\tests -q
.\.venv\Scripts\python.exe -m pytest packages\strategy_manager\tests -q
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests -q
.\.venv\Scripts\python.exe -m pytest tests -q -m archive
.\.venv\Scripts\ruff.exe check src tests packages\strategy_evaluator packages\strategy_manager packages\paper_trading_engine\src packages\paper_trading_engine\tests
.\.venv\Scripts\python.exe -m compileall -q src packages\strategy_evaluator\src
git diff --check
```

Expected: every command exits zero. `git status --short` shows only the preserved unrelated
`configs/backtest_windows/2026.json` modification before documentation is staged.

- [ ] **Step 4: Commit documentation**

```powershell
git add README.md docs\RESEARCH_HANDOFF.md docs\DEVELOPMENT_HANDOFF.md
git commit -m "docs: hand off strategy evaluation operations"
```

- [ ] **Step 5: Inspect branch without merging or pushing**

```powershell
git status --short
git log --oneline master..HEAD
```

Expected: implementation is committed on `codex/strategy-evaluator`; the user-owned window file remains
unstaged. Merging to master and pushing remain explicit user decisions.
