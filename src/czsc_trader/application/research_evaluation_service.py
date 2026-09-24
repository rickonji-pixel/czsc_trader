"""Researcher-facing TDR evaluation entry backed by the shared Harness."""

from __future__ import annotations

from .context import RepositoryContext
from .evaluation_service import evaluate_experiment
from .results import CommandResult


def evaluate_research_experiment(
    context: RepositoryContext, experiment_id: str
) -> CommandResult:
    """Evaluate an experiment without candidate admission or governance actions."""

    evaluated = evaluate_experiment(context, experiment_id)
    return CommandResult(
        evaluated.status,
        "research.evaluate",
        evaluated.result,
        evaluated.artifacts,
        evaluated.warnings,
    )
