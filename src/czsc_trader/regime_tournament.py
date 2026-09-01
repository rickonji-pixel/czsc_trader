"""Pure scoring primitives for the six-candidate regime tournament."""

from __future__ import annotations

import math

import pandas as pd


def metric_score(candidate: float, baseline: float, *, tolerance: float) -> float:
    """Score one higher-is-better metric as win, tie, or loss."""
    candidate_value = float(candidate)
    baseline_value = float(baseline)
    if not math.isfinite(candidate_value) or not math.isfinite(baseline_value):
        raise ValueError("tournament metrics must be finite")
    difference = candidate_value - baseline_value
    if difference > float(tolerance):
        return 1.0
    if abs(difference) <= float(tolerance):
        return 0.5
    return 0.0


def rank_total_scores(rows: pd.DataFrame) -> pd.DataFrame:
    """Rank candidate totals with dense ties and stable ID display order."""
    required = {"candidate_id", "total_score"}
    if not required <= set(rows.columns):
        raise ValueError(f"score rows missing columns: {sorted(required - set(rows.columns))}")
    ranked = rows.sort_values(
        ["total_score", "candidate_id"],
        ascending=[False, True],
        kind="stable",
    ).reset_index(drop=True)
    ranked.insert(
        0,
        "rank",
        ranked["total_score"].rank(method="dense", ascending=False).astype(int),
    )
    return ranked
