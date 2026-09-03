"""Deterministic statistics for strategy robustness validation."""

from __future__ import annotations

import numpy as np
import pandas as pd
from strategy_evaluator import (
    ReturnMatrixEvidence,
    annualized_sharpe as _se_annualized_sharpe,
    cscv_pbo as _se_cscv_pbo,
    deflated_sharpe_ratio as _se_deflated_sharpe_ratio,
)


def annualized_sharpe(returns: np.ndarray | pd.Series) -> float:
    """Return the zero-risk-rate annualized Sharpe for daily returns."""
    return _se_annualized_sharpe(np.asarray(returns, dtype=float))


def contiguous_blocks(length: int, block_count: int) -> tuple[np.ndarray, ...]:
    """Split row positions into non-empty, ordered, near-equal blocks."""
    if block_count < 2 or block_count % 2:
        raise ValueError("block_count must be an even integer of at least two")
    if length < block_count:
        raise ValueError("length must be at least block_count")
    return tuple(np.asarray(block, dtype=int) for block in np.array_split(np.arange(length), block_count))


def candidate_sharpes(frame: pd.DataFrame) -> pd.Series:
    """Calculate every candidate Sharpe on the shared 252-session scale."""
    if frame.empty:
        raise ValueError("candidate return frame must not be empty")
    sharpes = frame.apply(lambda column: annualized_sharpe(column.to_numpy()), axis=0)
    return sharpes.replace([np.inf, -np.inf], np.nan)


def cscv_pbo(
    returns: pd.DataFrame, block_count: int = 10
) -> tuple[pd.DataFrame, dict[str, float | int]]:
    """Compatibility adapter for the SE-owned CSCV implementation."""
    matrix = returns.astype(float)
    evidence = ReturnMatrixEvidence(
        tuple(map(str, matrix.index)), tuple(map(str, matrix.columns)),
        tuple(tuple(float(value) for value in row) for row in matrix.to_numpy()), "0" * 64,
    )
    result = _se_cscv_pbo(evidence, block_count)
    records = []
    for item in result.splits:
        row = item.to_dict()
        row["training_blocks"] = ",".join(map(str, item.training_blocks))
        row["validation_blocks"] = ",".join(map(str, item.validation_blocks))
        records.append(row)
    details = pd.DataFrame.from_records(records)
    summary: dict[str, float | int] = {
        "block_count": result.block_count,
        "split_count": len(result.splits),
        "candidate_count": result.candidate_count,
        "pbo": result.pbo,
        "median_validation_percentile": result.median_validation_percentile,
        "median_logit": result.median_logit,
    }
    return details, summary


def deflated_sharpe_ratio(
    selected_returns: pd.Series, trial_sharpes: pd.Series
) -> dict[str, float | int]:
    """Compatibility adapter for the SE-owned DSR implementation."""
    result = _se_deflated_sharpe_ratio(
        selected_returns.to_numpy(dtype=float), trial_sharpes.to_numpy(dtype=float),
        float(len(trial_sharpes)),
    )
    return {
        **result.to_dict(),
        "trial_count": int(result.trial_count),
        "dsr_probability": result.probability,
    }


def parameter_geometry(
    candidates: pd.DataFrame,
    selected_id: int,
    parameter_columns: tuple[str, ...],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Measure grid distance and metric degradation around one candidate."""
    if "candidate_id" not in candidates or not parameter_columns:
        raise ValueError("candidate_id and parameter columns are required")
    surface = candidates.copy()
    selected_rows = surface.loc[surface["candidate_id"].eq(selected_id)]
    if len(selected_rows) != 1:
        raise ValueError("selected candidate must appear exactly once")
    selected = selected_rows.iloc[0]
    scaled_deltas: list[np.ndarray] = []
    for column in parameter_columns:
        values = np.sort(surface[column].astype(float).unique())
        if len(values) < 2:
            raise ValueError(f"parameter {column} must contain at least two levels")
        steps = np.diff(values)
        step = float(np.min(steps[steps > 0.0]))
        scaled_deltas.append(
            np.abs(surface[column].astype(float).to_numpy() - float(selected[column])) / step
        )
    delta_matrix = np.column_stack(scaled_deltas)
    surface["manhattan_distance"] = delta_matrix.sum(axis=1)
    surface["euclidean_distance"] = np.sqrt(np.square(delta_matrix).sum(axis=1))
    protected = {"candidate_id", *parameter_columns, "manhattan_distance", "euclidean_distance"}
    for column in tuple(surface.columns):
        if column in protected or not pd.api.types.is_numeric_dtype(surface[column]):
            continue
        surface[f"{column}_delta_from_selected"] = surface[column].astype(float) - float(selected[column])
    neighbors = surface.loc[
        surface["manhattan_distance"].isin((1.0, 2.0))
    ].sort_values(["manhattan_distance", "candidate_id"], kind="stable")
    return surface.sort_values("candidate_id", kind="stable").reset_index(drop=True), neighbors.reset_index(drop=True)


def cyclic_shifts(values: pd.Series) -> tuple[pd.Series, ...]:
    """Enumerate every non-identity circular shift while preserving the index."""
    if len(values) < 2:
        raise ValueError("cyclic shifts require at least two observations")
    array = values.to_numpy(copy=True)
    shifts = tuple(
        pd.Series(np.roll(array, -lag), index=values.index, name=values.name)
        for lag in range(1, len(values))
    )
    identities = {tuple(item.tolist()) for item in shifts}
    if len(identities) != len(shifts):
        raise ValueError("cyclic shifts are not unique for this signal sequence")
    return shifts


def placebo_signal_paths(
    values: pd.Series, initial_target: float
) -> tuple[tuple[int, pd.Series, float], ...]:
    """Return observed and shifted paths with one common causal initial state."""
    initial = float(initial_target)
    if not np.isfinite(initial) or not 0.0 <= initial <= 1.0:
        raise ValueError("initial_target must be finite and between zero and one")
    return ((0, values.copy(), initial),) + tuple(
        (lag, shifted, initial)
        for lag, shifted in enumerate(cyclic_shifts(values), start=1)
    )
