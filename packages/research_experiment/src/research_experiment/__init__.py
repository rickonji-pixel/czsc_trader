"""Research Experiment (REX) public contracts."""

from .contracts import (
    ExperimentArtifact,
    ExperimentCapabilities,
    ExperimentCapability,
    ExperimentContext,
    ExperimentDefinition,
    ExperimentDependency,
    ExperimentMode,
    ExperimentOutcome,
    ExperimentResources,
    ExperimentResult,
    ExperimentTrace,
    ExperimentWorkspace,
    ResearchExperiment,
)
from .loader import (
    ExperimentBinding,
    LoadedExperiment,
    experiment_source_sha256,
    load_experiment,
)

__version__ = "0.1.0"

__all__ = [
    "ExperimentArtifact",
    "ExperimentBinding",
    "ExperimentCapabilities",
    "ExperimentCapability",
    "ExperimentContext",
    "ExperimentDefinition",
    "ExperimentDependency",
    "ExperimentMode",
    "ExperimentOutcome",
    "ExperimentResources",
    "ExperimentResult",
    "ExperimentTrace",
    "ExperimentWorkspace",
    "LoadedExperiment",
    "ResearchExperiment",
    "experiment_source_sha256",
    "load_experiment",
]
