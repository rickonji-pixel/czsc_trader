from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import shutil

from dataflows import DataRequest, Dataflows
import pandas as pd
import pytest
from strategy_runtime import StrategyCandidate

from research_experiment import (
    ExperimentCapabilities,
    ExperimentCapability,
    ExperimentDefinition,
    ExperimentDependency,
    ExperimentMode,
    ExperimentOutcome,
    ExperimentResources,
    ExperimentResult,
    ExperimentWorkspace,
    ResearchExperiment,
    load_experiment,
)
from czsc_trader.research_tools import (
    EvaluationCost,
    EvaluationRequest,
    EvaluationResult,
    EvaluationWindow,
    create_experiment_context,
    execute_experiment,
)


FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "s008_research_cases"
    / "20260924_S008_EX99"
)


def _definition(
    *, capabilities: ExperimentCapabilities = ExperimentCapabilities()
) -> ExperimentDefinition:
    return ExperimentDefinition(
        schema_version=1,
        experiment_id="20260924_S008_EX98",
        strategy_id="S008",
        mode=ExperimentMode.DISCOVERY,
        research_question="Does the public experiment boundary reject undeclared behavior?",
        hypothesis="Every sensitive operation is checked before execution.",
        falsification_conditions=("An undeclared operation reaches its provider",),
        development_cutoff=date(2026, 9, 2),
        random_seed=98,
        allowed_datasets=("etf.ohlcv",),
        capabilities=capabilities,
    )


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-09-01", "2026-09-02"]),
            "Open": [10.0, 10.2],
            "High": [10.3, 10.4],
            "Low": [9.9, 10.1],
            "Close": [10.2, 10.3],
            "Volume": [100.0, 110.0],
            "Amount": [1_020.0, 1_133.0],
        }
    )


def _flows(calls: list[DataRequest] | None = None) -> Dataflows:
    def provider(request: DataRequest):
        if calls is not None:
            calls.append(request)
        return _frame(), {
            "vendor": "synthetic-test",
            "vendor_symbol": request.symbol,
            "asset_type": "etf",
            "period": "daily",
            "adjustment": "hfq",
        }

    return Dataflows({"etf.ohlcv": provider})


def _workspace(functional_repo: Path, name: str) -> ExperimentWorkspace:
    return ExperimentWorkspace(functional_repo / ".tmp" / name, functional_repo)


def _candidate() -> StrategyCandidate:
    return StrategyCandidate(
        strategy_family_id="S008",
        candidate_id="synthetic",
        payload={"runtime": {}, "parameters": {}},
    )


def test_s008_fixture_loads_and_executes_through_public_context(
    functional_repo: Path,
) -> None:
    experiment = load_experiment(FIXTURE_ROOT)
    context = create_experiment_context(
        experiment.definition,
        repository_root=functional_repo,
        dataflows=_flows(),
        workspace=_workspace(functional_repo, "experiment-fixture"),
        resources=ExperimentResources(
            max_workers=4,
            max_evaluations=8,
            random_seed=experiment.definition.random_seed,
        ),
    )

    result = execute_experiment(experiment, context)

    assert result.outcome is ExperimentOutcome.PASS
    assert result.facts == {"rows": 2, "mean_close": 10.25, "max_workers": 4}
    assert result.artifacts[0].kind == "research-summary"
    assert json.loads(context.workspace.path("summary.json").read_text(encoding="utf-8")) == {
        "max_workers": 4,
        "mean_close": 10.25,
        "rows": 2,
    }
    assert context.trace.capabilities == (ExperimentCapability.SEARCH_PARAMETERS,)
    assert context.trace.operations == ("data.fetch",)
    assert context.trace.data_requests[0]["identity"]["dataset"] == "etf.ohlcv"


def test_loader_rejects_source_tampering(functional_repo: Path) -> None:
    target = functional_repo / ".tmp" / "tampered" / FIXTURE_ROOT.name
    target.parent.mkdir(parents=True)
    shutil.copytree(FIXTURE_ROOT, target)
    source = target / "experiment.py"
    source.write_text(source.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")

    with pytest.raises(ValueError, match="source SHA-256 differs"):
        load_experiment(target)


def test_data_adapter_blocks_undeclared_dataset_before_provider(
    functional_repo: Path,
) -> None:
    calls: list[DataRequest] = []
    definition = _definition()
    context = create_experiment_context(
        definition,
        repository_root=functional_repo,
        dataflows=_flows(calls),
        workspace=_workspace(functional_repo, "undeclared-dataset"),
        resources=ExperimentResources(max_workers=1, random_seed=98),
    )

    with pytest.raises(PermissionError, match="dataset was not declared"):
        context.data.fetch(
            DataRequest(
                dataset="fx.fxcm_daily",
                symbol="XAU/USD",
                start="2026-09-01",
                end="2026-09-02",
                required_cutoff="2026-09-02",
            )
        )

    assert calls == []


def test_data_adapter_blocks_future_data_before_provider(functional_repo: Path) -> None:
    calls: list[DataRequest] = []
    definition = _definition()
    context = create_experiment_context(
        definition,
        repository_root=functional_repo,
        dataflows=_flows(calls),
        workspace=_workspace(functional_repo, "future-data"),
        resources=ExperimentResources(max_workers=1, random_seed=98),
    )

    with pytest.raises(PermissionError, match="development cutoff"):
        context.data.fetch(
            DataRequest(
                dataset="etf.ohlcv",
                symbol="518880.SH",
                start="2026-09-01",
                end="2026-09-03",
                required_cutoff="2026-09-03",
            )
        )

    assert calls == []


@pytest.mark.parametrize(
    ("real_returns", "sealed_validation", "missing"),
    [
        (True, False, "reads_real_returns"),
        (False, True, "reads_sealed_validation"),
    ],
)
def test_data_adapter_blocks_undeclared_sensitive_access_before_provider(
    functional_repo: Path,
    real_returns: bool,
    sealed_validation: bool,
    missing: str,
) -> None:
    calls: list[DataRequest] = []
    definition = _definition()
    context = create_experiment_context(
        definition,
        repository_root=functional_repo,
        dataflows=_flows(calls),
        workspace=_workspace(functional_repo, f"missing-{missing}"),
        resources=ExperimentResources(max_workers=1, random_seed=98),
        real_returns=real_returns,
        sealed_validation=sealed_validation,
    )

    with pytest.raises(PermissionError, match=missing):
        context.data.fetch(
            DataRequest(
                dataset="etf.ohlcv",
                symbol="518880.SH",
                start="2026-09-01",
                end="2026-09-02",
                required_cutoff="2026-09-02",
            )
        )

    assert calls == []


def test_context_tracks_runtime_and_evaluation_public_adapters(
    functional_repo: Path,
) -> None:
    candidate = _candidate()
    runtime_calls: list[str] = []
    evaluation_calls: list[str] = []

    class FakeRuntime:
        def describe(self, source, **kwargs):
            del source, kwargs
            runtime_calls.append("describe")
            return "runtime-definition"

    def evaluator(request: EvaluationRequest) -> EvaluationResult:
        evaluation_calls.append(request.experiment_id)
        return EvaluationResult(runs=())

    definition = _definition(
        capabilities=ExperimentCapabilities(reads_real_returns=True)
    )
    context = create_experiment_context(
        definition,
        repository_root=functional_repo,
        dataflows=_flows(),
        workspace=_workspace(functional_repo, "platform-adapters"),
        resources=ExperimentResources(max_workers=1, random_seed=98),
        runtime=FakeRuntime(),
        evaluator=evaluator,
        real_returns=True,
    )
    request = EvaluationRequest(
        repository_root=functional_repo,
        experiment_id=definition.experiment_id,
        strategy=candidate,
        runtime_binding={},
        symbol="518880.SH",
        asset_type="etf",
        windows=(EvaluationWindow("full", date(2026, 9, 1), date(2026, 9, 2)),),
        development_cutoff=definition.development_cutoff,
        initial_cash=1_000_000.0,
        costs=(EvaluationCost("main", 0.001),),
        execution_data=object(),
    )

    assert context.runtime.describe(candidate) == "runtime-definition"
    assert context.evaluation.evaluate(request).runs == ()
    assert runtime_calls == ["describe"]
    assert evaluation_calls == [definition.experiment_id]
    assert context.trace.capabilities == (ExperimentCapability.READ_REAL_RETURNS,)
    assert context.trace.operations == ("runtime.describe", "evaluation.evaluate")


class _CandidateExperiment(ResearchExperiment):
    def __init__(self, definition: ExperimentDefinition) -> None:
        self._definition = definition

    @property
    def definition(self) -> ExperimentDefinition:
        return self._definition

    def execute(self, context) -> ExperimentResult:
        del context
        return ExperimentResult(
            outcome=ExperimentOutcome.PASS,
            facts={"candidate": True},
            diagnostics={},
            candidate=_candidate(),
        )


def test_candidate_result_requires_declared_capability(functional_repo: Path) -> None:
    definition = _definition()
    context = create_experiment_context(
        definition,
        repository_root=functional_repo,
        dataflows=_flows(),
        workspace=_workspace(functional_repo, "candidate-capability"),
        resources=ExperimentResources(max_workers=1, random_seed=98),
    )

    with pytest.raises(PermissionError, match="creates_candidate"):
        execute_experiment(_CandidateExperiment(definition), context)


def test_workspace_rejects_escape_and_detects_artifact_change(
    functional_repo: Path,
) -> None:
    workspace = _workspace(functional_repo, "workspace-boundary")
    with pytest.raises(ValueError, match="experiment workspace"):
        workspace.path("../outside.json")
    target = workspace.path("facts/result.json")
    target.write_text("{}", encoding="utf-8")
    artifact = workspace.register_artifact("facts/result.json", "facts")
    target.write_text('{"changed":true}', encoding="utf-8")

    with pytest.raises(ValueError, match="artifact hash differs"):
        workspace.validate_artifact(artifact)


def test_definition_and_result_freeze_json_payloads() -> None:
    definition = _definition()
    result = ExperimentResult(
        outcome=ExperimentOutcome.INCONCLUSIVE,
        facts={"nested": {"values": [1, 2]}},
        diagnostics={},
    )

    assert len(definition.sha256) == 64
    assert result.facts["nested"]["values"] == (1, 2)
    with pytest.raises(TypeError):
        result.facts["new"] = True
    with pytest.raises(ValueError, match="finite JSON"):
        ExperimentResult(
            outcome=ExperimentOutcome.FAIL,
            facts={"bad": float("nan")},
            diagnostics={},
        )
    with pytest.raises(ValueError, match="must be exact"):
        ExperimentDependency("optuna", ">=4.0")
