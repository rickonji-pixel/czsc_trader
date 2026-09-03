import math
from collections import Counter
from collections.abc import Sequence

from .models import CandidateDescriptor, EvaluationProtocol, MetricObservation, MetricStatus, TrialRecord, ValidationError
from .standards import MarginSet, resolve_margins


def validate_protocol(
    protocol: EvaluationProtocol,
    candidates: Sequence[CandidateDescriptor],
    observations: Sequence[MetricObservation],
    trials: Sequence[TrialRecord],
) -> MarginSet:
    margins = resolve_margins(protocol)
    if protocol.schema_version != 1:
        raise ValidationError("unsupported schema_version", "UNSUPPORTED_SCHEMA")
    if not protocol.decision_windows or "full" not in protocol.decision_windows:
        raise ValidationError("decision_windows must include full", "INVALID_WINDOWS")
    ids = [item.candidate_id for item in candidates]
    if len(ids) != len(set(ids)):
        raise ValidationError("candidate IDs must be unique", "DUPLICATE_CANDIDATE")
    incumbents = [item for item in candidates if item.is_incumbent]
    if len(incumbents) != 1 or incumbents[0].candidate_id != protocol.incumbent_id:
        raise ValidationError("exactly one incumbent must match protocol", "INVALID_INCUMBENT")
    if incumbents[0].strategy_hash != protocol.incumbent_hash:
        raise ValidationError("incumbent hash mismatch", "HASH_MISMATCH")
    for item in candidates:
        if item.execution_policy_hash != protocol.execution_policy_hash:
            raise ValidationError(f"execution policy hash mismatch for {item.candidate_id}", "EXECUTION_HASH_MISMATCH")
    trial_ids = Counter(item.candidate_id for item in trials)
    if any(trial_ids[candidate_id] < 1 for candidate_id in ids):
        raise ValidationError("trial ledger must cover every candidate", "INCOMPLETE_TRIAL_LEDGER")
    if any(item.candidate_id not in ids for item in trials):
        raise ValidationError("trial ledger references unknown candidate", "UNKNOWN_TRIAL_CANDIDATE")
    observed = {(item.candidate_id, item.window_id) for item in observations}
    for window in protocol.decision_windows:
        if (protocol.incumbent_id, window) not in observed:
            raise ValidationError(f"incumbent missing decision window {window}", "INCOMPLETE_INCUMBENT")
    requirements = {item.metric for item in protocol.target_requirements}
    available = {name for item in observations for name, _ in item.objective_values}
    if requirements - available:
        raise ValidationError(f"unknown target requirements: {sorted(requirements - available)}", "UNKNOWN_TARGET")
    for item in observations:
        values = (item.net_cagr, item.total_return, item.max_drawdown, item.calmar, item.profit_factor, item.turnover, item.cost_drag)
        if any(value is not None and not math.isfinite(value) for value in values):
            raise ValidationError("metric values must be finite", "NONFINITE_METRIC")
        if item.max_drawdown > 0:
            raise ValidationError("max_drawdown must be non-positive", "INVALID_DRAWDOWN")
        if item.closed_trades < 0:
            raise ValidationError("closed_trades must be non-negative", "INVALID_TRADE_COUNT")
        for value, status, name in ((item.calmar, item.calmar_status, "calmar"), (item.profit_factor, item.profit_factor_status, "profit_factor")):
            if status is MetricStatus.VALID and value is None:
                raise ValidationError(f"{name} VALID requires a value", "STATUS_VALUE_MISMATCH")
            if status is MetricStatus.UNAVAILABLE and value is not None:
                raise ValidationError(f"{name} UNAVAILABLE requires no value", "STATUS_VALUE_MISMATCH")
        if item.measurement_tier not in {"SCREENING", "FORMAL", "STRESS"}:
            raise ValidationError(f"unsupported measurement tier: {item.measurement_tier}", "INVALID_TIER")
    return margins
