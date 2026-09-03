"""Pure primitives for range-weight parameter-platform research."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd


GROUP_NAMES = ("structure", "trend", "volume_position")


def dirichlet_weight_candidates(
    anchors: Mapping[str, pd.Series],
    *,
    samples_per_anchor: int,
    concentrations: Mapping[str, float],
    seed: int,
) -> pd.DataFrame:
    """Generate deterministic positive simplex candidates around named anchors."""
    if not anchors or set(anchors) != set(concentrations):
        raise ValueError("anchors and concentrations must contain the same names")
    if not isinstance(samples_per_anchor, int) or samples_per_anchor < 0:
        raise ValueError("samples_per_anchor must be a non-negative integer")
    first = next(iter(anchors.values()))
    factors = list(first.index)
    if not factors or len(factors) != len(set(factors)):
        raise ValueError("anchors must contain the same positive factor weights")
    normalized: dict[str, np.ndarray] = {}
    for name, anchor in anchors.items():
        values = anchor.reindex(factors).to_numpy(dtype=float)
        concentration = float(concentrations[name])
        if (
            list(anchor.index) != factors
            or not np.isfinite(values).all()
            or (values <= 0.0).any()
            or not np.isfinite(concentration)
            or concentration <= 0.0
        ):
            raise ValueError("anchors must contain the same positive factor weights")
        normalized[name] = values / values.sum()
    rng = np.random.default_rng(int(seed))
    rows: list[dict[str, object]] = []
    for name, anchor in normalized.items():
        for sample_index, values in enumerate(
            [anchor, *rng.dirichlet(anchor * float(concentrations[name]), samples_per_anchor)]
        ):
            rows.append(
                {
                    "candidate_id": len(rows),
                    "anchor_name": name,
                    "sample_index": sample_index,
                    "is_anchor": sample_index == 0,
                    **dict(zip(factors, values, strict=True)),
                }
            )
    return pd.DataFrame(rows)


def project_group_shares(
    base_weights: pd.Series,
    groups: Mapping[str, Sequence[str]],
    shares: Mapping[str, float],
) -> pd.Series:
    """Project exact group L1 shares while preserving within-group weight ratios."""
    if set(groups) != set(GROUP_NAMES) or set(shares) != set(GROUP_NAMES):
        raise ValueError("groups and shares must contain the three frozen factor groups")
    membership = [str(item) for name in GROUP_NAMES for item in groups[name]]
    if len(membership) != len(set(membership)) or set(membership) != set(base_weights.index):
        raise ValueError("factor group membership differs from base weights")
    share_values = np.asarray([shares[name] for name in GROUP_NAMES], dtype=float)
    if not np.isfinite(share_values).all() or (share_values < 0.0).any():
        raise ValueError("group shares must be finite and non-negative")
    if not np.isclose(share_values.sum(), 1.0, rtol=0.0, atol=1e-12):
        raise ValueError("group shares must sum to one")
    projected = base_weights.astype(float).copy()
    if not np.isfinite(projected.to_numpy()).all():
        raise ValueError("base weights must be finite")
    for name, share in zip(GROUP_NAMES, share_values, strict=True):
        members = list(groups[name])
        scale = float(projected.loc[members].abs().sum())
        if scale <= 0.0:
            raise ValueError(f"factor group {name} has zero L1 weight")
        projected.loc[members] = projected.loc[members].mul(float(share) / scale)
    if not np.isclose(projected.abs().sum(), 1.0, rtol=0.0, atol=1e-12):
        raise AssertionError("projected weights do not have unit L1 norm")
    projected.name = "weight"
    return projected


def pareto_layers(
    candidates: pd.DataFrame,
    metrics: Sequence[str],
    *,
    tolerance: float = 1e-12,
) -> pd.Series:
    """Return one-based non-dominated sorting layers for maximize-all metrics."""
    required = {"candidate_id", *map(str, metrics)}
    if not required <= set(candidates.columns):
        raise ValueError(f"candidate metrics missing columns: {sorted(required - set(candidates.columns))}")
    if candidates["candidate_id"].duplicated().any():
        raise ValueError("candidate ids must be unique")
    values = candidates.loc[:, list(metrics)].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("Pareto metrics must be finite")
    tol = float(tolerance)
    if not np.isfinite(tol) or tol < 0.0:
        raise ValueError("Pareto tolerance must be finite and non-negative")
    ids = candidates["candidate_id"].astype(int).to_numpy()
    remaining = list(range(len(candidates)))
    assigned: dict[int, int] = {}
    layer = 1
    while remaining:
        front: list[int] = []
        for current in remaining:
            dominated = False
            for other in remaining:
                if current == other:
                    continue
                no_worse = np.all(values[other] >= values[current] - tol)
                strictly_better = np.any(values[other] > values[current] + tol)
                if no_worse and strictly_better:
                    dominated = True
                    break
            if not dominated:
                front.append(current)
        if not front:
            raise AssertionError("Pareto sorting produced an empty front")
        for position in front:
            assigned[int(ids[position])] = layer
        front_set = set(front)
        remaining = [position for position in remaining if position not in front_set]
        layer += 1
    return pd.Series(assigned, dtype=int, name="pareto_layer").sort_index()


def simplex_components(
    candidates: pd.DataFrame,
    member_ids: Sequence[int],
    *,
    step: float,
    tolerance: float = 1e-12,
) -> list[list[int]]:
    """Return deterministic connected components under one simplex-grid transfer."""
    columns = ("structure_share", "trend_share", "volume_share")
    required = {"candidate_id", *columns}
    if not required <= set(candidates.columns):
        raise ValueError(f"candidate shares missing columns: {sorted(required - set(candidates.columns))}")
    indexed = candidates.set_index("candidate_id")
    members = sorted(map(int, member_ids))
    if len(members) != len(set(members)) or not set(members) <= set(indexed.index):
        raise ValueError("component member ids are invalid")
    delta = float(step)
    tol = float(tolerance)
    if not np.isfinite(delta) or delta <= 0.0:
        raise ValueError("simplex step must be positive and finite")

    def adjacent(left: int, right: int) -> bool:
        difference = np.abs(
            indexed.loc[left, list(columns)].to_numpy(dtype=float)
            - indexed.loc[right, list(columns)].to_numpy(dtype=float)
        )
        return bool(
            np.all(difference <= delta + tol)
            and np.isclose(difference.sum(), 2.0 * delta, rtol=0.0, atol=tol)
        )

    pending = set(members)
    components: list[list[int]] = []
    while pending:
        seed = min(pending)
        pending.remove(seed)
        component = [seed]
        frontier = [seed]
        while frontier:
            current = frontier.pop(0)
            neighbors = sorted(value for value in pending if adjacent(current, value))
            for neighbor in neighbors:
                pending.remove(neighbor)
                frontier.append(neighbor)
                component.append(neighbor)
        components.append(sorted(component))
    return sorted(components, key=lambda values: (-len(values), values[0]))
