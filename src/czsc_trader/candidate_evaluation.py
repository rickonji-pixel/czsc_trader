"""Compatibility facade for the shared research-evaluation Harness.

New research code imports the public API from :mod:`czsc_trader.research_tools`.
TDR application services keep this module while their imports are migrated.
"""

from .research_tools.evaluation import (
    METRIC_SEMANTICS_VERSION,
    CandidateEvaluationContext,
    EvaluationBenchmark,
    EvaluationCost,
    EvaluationRequest,
    EvaluationResult,
    EvaluationRun,
    EvaluationWorkspace,
    EvaluationWindow,
    _observation,
    _scenario_settings,
    _snapshot,
    evaluate_candidate_payloads,
    evaluate_strategy,
    execute_candidate_replay,
    prepare_candidate_replays,
    prepare_evaluation_workspace,
)

__all__ = [
    "METRIC_SEMANTICS_VERSION",
    "CandidateEvaluationContext",
    "EvaluationBenchmark",
    "EvaluationCost",
    "EvaluationRequest",
    "EvaluationResult",
    "EvaluationRun",
    "EvaluationWorkspace",
    "EvaluationWindow",
    "_observation",
    "_scenario_settings",
    "_snapshot",
    "evaluate_candidate_payloads",
    "evaluate_strategy",
    "execute_candidate_replay",
    "prepare_candidate_replays",
    "prepare_evaluation_workspace",
]
