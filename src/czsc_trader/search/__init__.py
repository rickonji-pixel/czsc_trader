"""Auditable parameter-search adapters for strategy research."""

from .optuna_adapter import (
    CategoricalParameter,
    ConstraintSpec,
    FloatParameter,
    IntParameter,
    ObjectiveSpec,
    SearchEvaluation,
    SearchExecutionError,
    SearchIntegrationError,
    SearchResult,
    SearchSpec,
    SearchTrialRejected,
    run_search,
)

__all__ = [
    "CategoricalParameter",
    "ConstraintSpec",
    "FloatParameter",
    "IntParameter",
    "ObjectiveSpec",
    "SearchEvaluation",
    "SearchExecutionError",
    "SearchIntegrationError",
    "SearchResult",
    "SearchSpec",
    "SearchTrialRejected",
    "run_search",
]
