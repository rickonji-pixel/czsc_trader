from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .audit_models import AuditStatus, ParameterPoint
from .models import Record


@dataclass(frozen=True)
class NeighborhoodRow(Record):
    candidate_id: str
    behavior_hash: str
    distance: float
    eligible: bool
    pareto_layer: int | None
    worst_scores: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class NeighborhoodMetric(Record):
    metric: str
    champion_score: float
    median: float
    q1: float
    worst: float
    median_degradation: float


@dataclass(frozen=True)
class NeighborhoodAudit(Record):
    champion_id: str
    status: AuditStatus
    valid_neighbor_count: int
    neighbors: tuple[NeighborhoodRow, ...]
    metrics: tuple[NeighborhoodMetric, ...]
    reason_codes: tuple[str, ...] = ()


def audit_parameter_neighborhood(
    champion_id: str,
    points: tuple[ParameterPoint, ...],
    *,
    neighbor_limit: int = 20,
    minimum_valid: int = 10,
) -> NeighborhoodAudit:
    if neighbor_limit < 1 or minimum_valid < 1:
        raise ValueError("neighbor limits must be positive")
    champions = [item for item in points if item.candidate_id == champion_id]
    if len(champions) != 1:
        raise ValueError("champion must appear exactly once in parameter points")
    champion = champions[0]
    parameter_names = tuple(name for name, _ in champion.parameters)
    champion_values = np.asarray([value for _, value in champion.parameters], dtype=float)
    if not parameter_names or not np.isfinite(champion_values).all():
        raise ValueError("champion parameters must be finite and non-empty")

    nearest_by_behavior: dict[str, tuple[float, ParameterPoint]] = {}
    for point in points:
        if point.candidate_id == champion_id or point.is_incumbent:
            continue
        if tuple(name for name, _ in point.parameters) != parameter_names:
            raise ValueError("all parameter points must use the same ordered coordinates")
        values = np.asarray([value for _, value in point.parameters], dtype=float)
        scores = np.asarray([value for _, value in point.worst_scores], dtype=float)
        if not np.isfinite(values).all() or not np.isfinite(scores).all():
            raise ValueError("parameter points and profile scores must be finite")
        distance = float(np.abs(values - champion_values).sum())
        behavior = point.behavior_hash or point.candidate_id
        previous = nearest_by_behavior.get(behavior)
        key = (distance, point.candidate_id)
        if previous is None or key < (previous[0], previous[1].candidate_id):
            nearest_by_behavior[behavior] = (distance, point)
    selected = sorted(
        nearest_by_behavior.values(), key=lambda item: (item[0], item[1].candidate_id),
    )[:neighbor_limit]
    neighbors = tuple(
        NeighborhoodRow(
            point.candidate_id, point.behavior_hash or point.candidate_id, distance,
            point.eligible, point.pareto_layer, point.worst_scores,
        )
        for distance, point in selected
    )
    champion_scores = dict(champion.worst_scores)
    required_metrics = ("net_cagr", "max_drawdown", "calmar", "profit_factor")
    valid = [
        row for row in neighbors
        if row.eligible and all(metric in dict(row.worst_scores) for metric in required_metrics)
    ]
    summaries: list[NeighborhoodMetric] = []
    if all(metric in champion_scores for metric in required_metrics) and valid:
        for metric in required_metrics:
            values = np.asarray([dict(row.worst_scores)[metric] for row in valid], dtype=float)
            champion_score = champion_scores[metric]
            summaries.append(NeighborhoodMetric(
                metric, champion_score, float(np.median(values)),
                float(np.quantile(values, 0.25)), float(np.min(values)),
                float(champion_score - np.median(values)),
            ))
    status = AuditStatus.PASS if len(valid) >= minimum_valid and len(summaries) == 4 else AuditStatus.INSUFFICIENT
    reasons = () if status is AuditStatus.PASS else ("TOO_FEW_VALID_NEIGHBORS",)
    return NeighborhoodAudit(
        champion_id, status, len(valid), neighbors, tuple(summaries), reasons,
    )
