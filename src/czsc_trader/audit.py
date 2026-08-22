"""Explicit temporal assertions for the research output."""

from __future__ import annotations

import pandas as pd


def audit_no_lookahead(
    orders: pd.DataFrame,
    selections: pd.DataFrame,
    target_position: pd.Series,
) -> dict[str, int | str]:
    """Raise on any observable violation of the causal research contract."""
    if not target_position.isin([0.0, 1.0]).all():
        raise AssertionError("target_position contains values outside long/cash {0, 1}")
    if not target_position.index.is_monotonic_increasing or target_position.index.has_duplicates:
        raise AssertionError("target_position index must be unique and increasing")

    if not orders.empty:
        signal_dates = pd.to_datetime(orders["signal_date"])
        execution_dates = pd.to_datetime(orders["execution_date"])
        if signal_dates.isna().any() or not (signal_dates < execution_dates).all():
            raise AssertionError("every signal_date must be strictly before execution_date")

    selection_dates = pd.to_datetime(selections["as_of_date"])
    if not selection_dates.is_monotonic_increasing or selection_dates.duplicated().any():
        raise AssertionError("as_of_date must be unique and increasing")
    if selection_dates.dt.to_period("M").duplicated().any():
        raise AssertionError("there must be at most one parameter selection per month")
    trained = selections.loc[selections["train_end"].notna()].copy()
    if not trained.empty:
        train_end = pd.to_datetime(trained["train_end"])
        as_of = pd.to_datetime(trained["as_of_date"])
        if not (train_end < as_of).all():
            raise AssertionError("every train_end must be strictly before as_of_date")
        if trained["candidate_count"].nunique() != 1:
            raise AssertionError("candidate_count changed after walk-forward warm-up")

    return {
        "status": "PASS",
        "orders_checked": int(len(orders)),
        "selections_checked": int(len(selections)),
        "positions_checked": int(len(target_position)),
    }
