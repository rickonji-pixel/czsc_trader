"""Causal IC-basis and ETF relative-style event construction."""

from __future__ import annotations

import numpy as np
import pandas as pd


MECHANISM = "RELATIVE_STRENGTH_WITH_BASIS_CONFIRMATION"


def generate_basis_confirmation_events(
    relative_features: pd.DataFrame,
    basis_panel: pd.DataFrame,
    *,
    evaluation_start: str | pd.Timestamp,
    feature_lookback: int = 5,
    threshold_lookback: int = 60,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Generate a fixed price-strength plus improving-basis mechanism without returns."""

    features = relative_features.copy()
    basis = basis_panel.copy()
    for frame in (features, basis):
        frame["dt"] = pd.to_datetime(frame["dt"]).dt.normalize()
        frame.sort_values("dt", inplace=True)
        frame.reset_index(drop=True, inplace=True)
    if features["dt"].duplicated().any() or basis["dt"].duplicated().any():
        raise ValueError("feature and basis dates must be unique")
    required_features = {"dt", "relative_strength_5", "clean_20_session_window"}
    required_basis = {"dt", "raw_basis", "mapping_ts_code", "available_for_strategy_from"}
    if missing := sorted(required_features.difference(features.columns)):
        raise ValueError(f"relative features missing columns {missing}")
    if missing := sorted(required_basis.difference(basis.columns)):
        raise ValueError(f"basis panel missing columns {missing}")

    basis["available_for_strategy_from"] = pd.to_datetime(
        basis["available_for_strategy_from"]
    ).dt.normalize()
    known_from = basis["available_for_strategy_from"].dropna()
    source_dates = basis.loc[known_from.index, "dt"]
    if not (known_from > source_dates).all():
        raise ValueError("IC basis must only be usable after its source session")

    merged = features.merge(
        basis[["dt", "raw_basis", "mapping_ts_code"]],
        on="dt",
        how="inner",
        validate="one_to_one",
    )
    if len(merged) != len(features) or len(merged) != len(basis):
        raise ValueError("relative features and IC basis must have an exact calendar match")

    same_contract = merged["mapping_ts_code"].eq(merged["mapping_ts_code"].shift(1))
    raw_change = merged["raw_basis"].astype(float).diff()
    merged["basis_change_usable"] = raw_change.where(same_contract, 0.0)
    merged["basis_improvement_5"] = merged["basis_change_usable"].rolling(
        feature_lookback, min_periods=feature_lookback
    ).sum()
    strength = merged["relative_strength_5"].astype(float)
    basis_improvement = merged["basis_improvement_5"].astype(float)
    strength_threshold = strength.shift(1).rolling(
        threshold_lookback, min_periods=threshold_lookback
    ).median()
    basis_threshold = basis_improvement.shift(1).rolling(
        threshold_lookback, min_periods=threshold_lookback
    ).median()
    active = (
        strength.gt(strength_threshold)
        & basis_improvement.gt(basis_threshold)
        & merged["clean_20_session_window"].astype(bool)
    ).fillna(False)

    calendar = pd.DatetimeIndex(merged["dt"])
    next_session = pd.Series(calendar, index=calendar).shift(-1)
    signal_dates = calendar[active]
    events = pd.DataFrame(
        {
            "mechanism": MECHANISM,
            "signal_date": signal_dates,
            "event_date": next_session.reindex(signal_dates).to_numpy(),
            "signal_clock": "POST_CLOSE",
            "execution_clock": "NEXT_OPEN",
            "relative_strength_5": strength.loc[active].to_numpy(),
            "prior_strength_median": strength_threshold.loc[active].to_numpy(),
            "basis_improvement_5": basis_improvement.loc[active].to_numpy(),
            "prior_basis_median": basis_threshold.loc[active].to_numpy(),
        }
    ).dropna(subset=["event_date"])
    events = events.loc[events["event_date"] >= pd.Timestamp(evaluation_start)].reset_index(drop=True)

    activation = pd.Series(calendar.isin(pd.to_datetime(events["event_date"])), index=calendar)
    rolling = activation.astype(int).rolling(60, min_periods=60).sum()
    eligible = rolling.loc[rolling.index >= pd.Timestamp(evaluation_start)].dropna()
    median = float(eligible.median())
    p10 = float(eligible.quantile(0.10))
    density = pd.DataFrame(
        [
            {
                "mechanism": MECHANISM,
                "event_count": int(len(events)),
                "rolling_60_median": median,
                "rolling_60_p10": p10,
                "rolling_60_min": float(eligible.min()),
                "density_eligible": bool(12 <= median <= 20 and p10 >= 8),
            }
        ]
    )
    evidence = merged[
        [
            "dt",
            "mapping_ts_code",
            "raw_basis",
            "basis_change_usable",
            "basis_improvement_5",
            "relative_strength_5",
        ]
    ].copy()
    evidence["prior_strength_median"] = strength_threshold
    evidence["prior_basis_median"] = basis_threshold
    evidence["active"] = active
    if not np.isfinite(evidence["raw_basis"].to_numpy(dtype=float)).all():
        raise ValueError("raw basis must be finite")
    return events, density, evidence
