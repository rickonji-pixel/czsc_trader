from .models import (
    CandidateDescriptor,
    CandidateProfile,
    Decision,
    EvaluationProtocol,
    EvaluationResult,
    HealthEvidence,
    HealthStatus,
    MetricComparison,
    MetricObservation,
    MetricStatus,
    RankingResult,
    ShortlistResult,
    TargetRequirement,
    TrialRecord,
    ValidationError,
)
from .audit_models import (
    AuditFinding,
    AuditIdentity,
    AuditStatus,
    ChampionAuditRequest,
    ChampionAuditResult,
    ExecutionEvidence,
    ExecutionOrder,
    FactorEvent,
    ParameterPoint,
    ReturnMatrixEvidence,
    RiskLabel,
    StressScenario,
    StressScenarioResult,
)
from .standards import OPC_V1, OPC_V2, OPC_V3, EvaluationStandard, MarginSet, resolve_margins
from .validation import validate_protocol
from .evaluator import finalize_evaluation, rank_candidates, screen_candidates
from .noninferiority import compare_observation
from .pareto import pareto_layers
from .reporting import render_summary
from .search_bias import (
    CscvSplit,
    DsrBundle,
    DsrEstimate,
    SearchBiasResult,
    annualized_sharpe,
    calculate_dsr_bundle,
    cscv_pbo,
    deflated_sharpe_ratio,
    effective_trial_count,
)
from .bootstrap import (
    AbsoluteBootstrap,
    BootstrapComparison,
    BootstrapInterval,
    BootstrapMetric,
    PerformanceMetrics,
    audit_pairwise_bootstrap,
    paired_stationary_bootstrap,
    performance_metrics,
    stationary_bootstrap_performance,
)
from .neighborhood import (
    NeighborhoodAudit,
    NeighborhoodMetric,
    NeighborhoodRow,
    audit_parameter_neighborhood,
)
from .engineering_audit import (
    StressAudit,
    StressComparison,
    audit_execution,
    audit_reproducibility,
    audit_stress_results,
    audit_trial_ledger,
    legacy_health_evidence,
    required_stress_scenarios,
)
from .champion_audit import (
    audit_provisional_champion,
    hash_audit_data,
    hash_candidate_pool,
    hash_execution_evidence,
    hash_return_matrix,
)
from .replay_audit import (
    ReplayAuditResult,
    ReplayEvidence,
    audit_replay,
    hash_replay_evidence,
)
from .candidate_readiness import (
    CandidateReadinessDecision,
    CandidateReadinessRequest,
    CandidateReadinessResult,
    assess_research_candidate,
)

__version__ = "0.1.0"

__all__ = [
    "CandidateDescriptor", "CandidateProfile", "Decision", "EvaluationProtocol",
    "EvaluationResult", "HealthEvidence", "HealthStatus", "MetricComparison",
    "MetricObservation", "MetricStatus", "RankingResult", "ShortlistResult",
    "TargetRequirement", "TrialRecord", "ValidationError",
    "AuditFinding", "AuditIdentity", "AuditStatus", "ChampionAuditRequest",
    "ChampionAuditResult", "ExecutionEvidence", "ExecutionOrder", "FactorEvent",
    "ParameterPoint", "ReturnMatrixEvidence", "RiskLabel", "StressScenario",
    "StressScenarioResult", "EvaluationStandard", "MarginSet", "OPC_V1", "OPC_V2",
    "OPC_V3", "resolve_margins", "validate_protocol",
    "compare_observation", "pareto_layers", "rank_candidates", "screen_candidates",
    "finalize_evaluation", "render_summary",
    "CscvSplit", "DsrBundle", "DsrEstimate", "SearchBiasResult",
    "annualized_sharpe", "calculate_dsr_bundle", "cscv_pbo",
    "deflated_sharpe_ratio", "effective_trial_count",
    "AbsoluteBootstrap", "BootstrapComparison", "BootstrapInterval", "BootstrapMetric",
    "PerformanceMetrics", "audit_pairwise_bootstrap", "paired_stationary_bootstrap",
    "performance_metrics", "stationary_bootstrap_performance",
    "NeighborhoodAudit", "NeighborhoodMetric", "NeighborhoodRow",
    "audit_parameter_neighborhood",
    "StressAudit", "StressComparison", "audit_execution", "audit_reproducibility",
    "audit_stress_results", "audit_trial_ledger", "required_stress_scenarios",
    "legacy_health_evidence",
    "audit_provisional_champion", "hash_audit_data", "hash_candidate_pool",
    "hash_execution_evidence", "hash_return_matrix",
    "ReplayAuditResult", "ReplayEvidence", "audit_replay", "hash_replay_evidence",
    "CandidateReadinessDecision", "CandidateReadinessRequest",
    "CandidateReadinessResult", "assess_research_candidate",
]
