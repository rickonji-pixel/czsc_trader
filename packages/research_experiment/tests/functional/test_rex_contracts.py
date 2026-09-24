"""REX package boundary tests."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest
import research_experiment
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
    LoadedExperiment,
    ResearchExperiment,
    experiment_source_sha256,
    load_experiment,
)


def _definition() -> ExperimentDefinition:
    return ExperimentDefinition(
        schema_version=1,
        experiment_id="20260924_S008_EX99",
        strategy_id="S008",
        mode=ExperimentMode.DISCOVERY,
        research_question="Can REX load a standalone experiment?",
        hypothesis="The declared source closure is sufficient.",
        falsification_conditions=("The implementation cannot be loaded",),
        development_cutoff=date(2026, 9, 2),
        random_seed=99,
        allowed_datasets=("etf.ohlcv",),
        capabilities=ExperimentCapabilities(searches_parameters=True),
    )


def test_rex_has_no_tdr_imports() -> None:
    package_root = Path(research_experiment.__file__).resolve().parent

    assert ResearchExperiment.__module__.startswith("research_experiment.")
    for source in package_root.glob("*.py"):
        assert "czsc_trader" not in source.read_text(encoding="utf-8")


def test_loaded_experiment_cannot_be_constructed_directly() -> None:
    with pytest.raises(TypeError, match="only be created by load_experiment"):
        LoadedExperiment()


def test_contracts_freeze_payloads_and_validate_resources(tmp_path: Path) -> None:
    result = ExperimentResult(
        outcome=ExperimentOutcome.INCONCLUSIVE,
        facts={"nested": {"values": [1, 2]}},
        diagnostics={},
    )
    repository_root = tmp_path / "repo"
    workspace = ExperimentWorkspace(
        repository_root / ".tmp" / "rex-test", repository_root
    )
    artifact_path = workspace.path("facts/result.json")
    artifact_path.write_text("{}", encoding="utf-8")
    artifact = workspace.register_artifact("facts/result.json", "facts")

    assert len(_definition().sha256) == 64
    assert result.facts["nested"]["values"] == (1, 2)
    assert artifact.sha256
    workspace.validate_artifact(artifact)
    with pytest.raises(TypeError):
        result.facts["changed"] = True
    with pytest.raises(ValueError, match="positive integer"):
        ExperimentResources(max_workers=0, random_seed=99)
    with pytest.raises(ValueError, match="must be exact"):
        ExperimentDependency("optuna", ">=4.0")
    with pytest.raises(ValueError, match="experiment workspace"):
        workspace.path("../outside.json")


def test_loader_rejects_undeclared_relative_source(tmp_path: Path) -> None:
    root = tmp_path / "S008" / "20260924_S008_EX99"
    root.mkdir(parents=True)
    source = root / "experiment.py"
    source.write_text(
        """from datetime import date
from research_experiment import (
    ExperimentCapabilities, ExperimentDefinition, ExperimentMode,
    ExperimentOutcome, ExperimentResult, ResearchExperiment,
)
from .helper import VALUE

class Experiment(ResearchExperiment):
    @property
    def definition(self):
        return ExperimentDefinition(
            schema_version=1,
            experiment_id='20260924_S008_EX99',
            strategy_id='S008',
            mode=ExperimentMode.DISCOVERY,
            research_question='Can undeclared source enter the closure?',
            hypothesis='REX rejects the undeclared helper.',
            falsification_conditions=('The helper is accepted',),
            development_cutoff=date(2026, 9, 2),
            random_seed=99,
            allowed_datasets=('etf.ohlcv',),
            capabilities=ExperimentCapabilities(),
        )

    def execute(self, context):
        return ExperimentResult(
            outcome=ExperimentOutcome.PASS,
            facts={'value': VALUE},
            diagnostics={},
        )
""",
        encoding="utf-8",
    )
    (root / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    binding = {
        "schema_version": 1,
        "module": "experiment",
        "qualname": "Experiment",
        "source_files": ["experiment.py"],
        "source_sha256": experiment_source_sha256(root, ("experiment.py",)),
    }
    (root / "experiment_binding.json").write_text(
        json.dumps(binding), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="undeclared source files"):
        load_experiment(root)


def test_capability_names_are_stable() -> None:
    assert tuple(item.value for item in ExperimentCapability) == (
        "reads_real_returns",
        "searches_parameters",
        "selects_parameters",
        "creates_candidate",
        "reads_sealed_validation",
    )
