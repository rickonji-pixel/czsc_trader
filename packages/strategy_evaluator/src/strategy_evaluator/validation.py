import math
import re
from collections import Counter
from collections.abc import Sequence
from datetime import date

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
    try:
        date.fromisoformat(protocol.development_cutoff)
    except ValueError as exc:
        raise ValidationError("development_cutoff must be YYYY-MM-DD", "INVALID_CUTOFF") from exc
    if protocol.shortlist_limit < 1:
        raise ValidationError("shortlist_limit must be positive", "INVALID_SHORTLIST")
    if len(protocol.decision_windows) != len(set(protocol.decision_windows)) or len(protocol.target_windows) != len(set(protocol.target_windows)):
        raise ValidationError("window IDs must be unique", "DUPLICATE_WINDOWS")
    if any(item.direction not in {"maximize", "minimize"} for item in protocol.target_requirements):
        raise ValidationError("target direction must be maximize or minimize", "INVALID_TARGET_DIRECTION")
    sha256 = re.compile(r"[0-9a-f]{64}")
    if not sha256.fullmatch(protocol.incumbent_hash) or not sha256.fullmatch(protocol.execution_policy_hash):
        raise ValidationError("protocol hashes must be lowercase SHA-256", "INVALID_HASH")
    if not protocol.decision_windows or "full" not in protocol.decision_windows:
        raise ValidationError("decision_windows must include full", "INVALID_WINDOWS")
    ids = [item.candidate_id for item in candidates]
    if len(ids) != len(set(ids)):
        raise ValidationError("candidate IDs must be unique", "DUPLICATE_CANDIDATE")
    if any(not sha256.fullmatch(item.candidate_hash) for item in candidates):
        raise ValidationError("candidate hashes must be lowercase SHA-256", "INVALID_HASH")
    incumbents = [item for item in candidates if item.is_incumbent]
    if len(incumbents) != 1 or incumbents[0].candidate_id != protocol.incumbent_id:
        raise ValidationError("exactly one incumbent must match protocol", "INVALID_INCUMBENT")
    if incumbents[0].candidate_hash != protocol.incumbent_hash:
        raise ValidationError("incumbent hash mismatch", "HASH_MISMATCH")
    for item in candidates:
        if item.execution_policy_hash != protocol.execution_policy_hash:
            raise ValidationError(f"execution policy hash mismatch for {item.candidate_id}", "EXECUTION_HASH_MISMATCH")
    trial_ids = Counter(item.candidate_id for item in trials)
    if len({item.trial_id for item in trials}) != len(trials):
        raise ValidationError("trial IDs must be unique", "DUPLICATE_TRIAL")
    if any(trial_ids[candidate_id] < 1 for candidate_id in ids):
        raise ValidationError("trial ledger must cover every candidate", "INCOMPLETE_TRIAL_LEDGER")
    if any(item.candidate_id not in ids for item in trials):
        raise ValidationError("trial ledger references unknown candidate", "UNKNOWN_TRIAL_CANDIDATE")
    candidate_hashes = {item.candidate_id: item.candidate_hash for item in candidates}
    if any(item.strategy_hash != candidate_hashes[item.candidate_id] for item in trials):
        raise ValidationError("trial hash does not match candidate", "TRIAL_HASH_MISMATCH")
    observation_keys = [(item.candidate_id, item.window_id, item.scenario_id, item.measurement_tier) for item in observations]
    if len(observation_keys) != len(set(observation_keys)):
        raise ValidationError("duplicate observation key", "DUPLICATE_OBSERVATION")
    if any(item.candidate_id not in ids for item in observations):
        raise ValidationError("observation references unknown candidate", "UNKNOWN_OBSERVATION_CANDIDATE")
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
