"""Causal age diagnostics for a persistent CZSC categorical state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd


def consecutive_state_age(indicator: pd.Series) -> pd.Series:
    """Count completed consecutive active days, resetting to zero when inactive."""
    active = indicator.fillna(0.0).astype(bool)
    age = np.zeros(len(active), dtype=int)
    current = 0
    for offset, value in enumerate(active.to_numpy(dtype=bool)):
        current = current + 1 if value else 0
        age[offset] = current
    return pd.Series(age, index=active.index, name="state_age")


def yearly_age_correlations(
    age: pd.Series,
    outcomes: pd.DataFrame,
    *,
    years: Sequence[int],
    horizons: Sequence[int],
) -> pd.DataFrame:
    """Calculate yearly rank relationships within active-state dates only."""
    values = age.reindex(outcomes.index).fillna(0).astype(int)
    rows: list[dict[str, object]] = []
    for year in map(int, years):
        for raw_horizon in horizons:
            horizon = int(raw_horizon)
            columns = [f"return_{horizon}", f"mae_{horizon}", f"max_drawdown_{horizon}"]
            mask = (
                (outcomes.index.year == year)
                & values.gt(0).to_numpy()
                & outcomes[columns].notna().all(axis=1).to_numpy()
            )
            sample = outcomes.loc[mask, columns]
            sample_age = values.loc[mask]
            rows.append(
                {
                    "year": year,
                    "horizon": horizon,
                    "active_n": int(mask.sum()),
                    "return_rho": float(sample_age.corr(sample[columns[0]], method="spearman")),
                    "mae_rho": float(sample_age.corr(sample[columns[1]], method="spearman")),
                    "max_drawdown_rho": float(sample_age.corr(sample[columns[2]], method="spearman")),
                }
            )
    return pd.DataFrame(rows)


def fixed_age_bin_summary(
    age: pd.Series,
    outcomes: pd.DataFrame,
    bins: Sequence[Mapping[str, object]],
    *,
    years: Sequence[int],
    horizons: Sequence[int],
) -> pd.DataFrame:
    """Summarize preregistered age ranges without choosing boundaries from outcomes."""
    values = age.reindex(outcomes.index).fillna(0).astype(int)
    rows: list[dict[str, object]] = []
    for year in map(int, years):
        for raw_horizon in horizons:
            horizon = int(raw_horizon)
            columns = [f"return_{horizon}", f"mae_{horizon}", f"max_drawdown_{horizon}"]
            valid = (outcomes.index.year == year) & outcomes[columns].notna().all(axis=1).to_numpy()
            for spec in bins:
                minimum = int(spec["minimum"])
                maximum = spec.get("maximum")
                mask = valid & values.ge(minimum).to_numpy()
                if maximum is not None:
                    mask &= values.le(int(maximum)).to_numpy()
                sample = outcomes.loc[mask, columns]
                rows.append(
                    {
                        "year": year,
                        "horizon": horizon,
                        "age_bin": str(spec["name"]),
                        "n": int(mask.sum()),
                        "mean_return": float(sample[columns[0]].mean()),
                        "mean_mae": float(sample[columns[1]].mean()),
                        "mean_max_drawdown": float(sample[columns[2]].mean()),
                    }
                )
    return pd.DataFrame(rows)


def classify_age_relationship(
    yearly: pd.DataFrame,
    *,
    primary_horizon: int,
    minimum_abs_median_rho: float,
) -> str:
    """Apply the exact cross-year monotonicity classification."""
    values = pd.to_numeric(
        yearly[yearly["horizon"].eq(int(primary_horizon))]["max_drawdown_rho"],
        errors="coerce",
    )
    if len(values) != 5 or values.isna().any() or values.eq(0.0).any():
        return "state_effect_not_explained_by_age"
    signs = np.sign(values.to_numpy(dtype=float)).astype(int)
    if len(set(signs.tolist())) != 1:
        return "state_effect_not_explained_by_age"
    if float(values.abs().median()) + 1e-15 < float(minimum_abs_median_rho):
        return "state_effect_not_explained_by_age"
    return "stable_age_relationship"
