"""Deterministic statistics for strategy robustness validation."""

from __future__ import annotations

from itertools import combinations

import numpy as np
import pandas as pd
from scipy.stats import norm


def annualized_sharpe(returns: np.ndarray | pd.Series) -> float:
    """Return the zero-risk-rate annualized Sharpe for daily returns."""
    values = np.asarray(returns, dtype=float)
    values = values[np.isfinite(values)]
    if values.size < 2:
        return float("nan")
    volatility = float(np.std(values, ddof=1))
    if volatility <= 0.0:
        mean = float(np.mean(values))
        if mean > 0.0:
            return float("inf")
        if mean < 0.0:
            return float("-inf")
        return 0.0
    return float(np.sqrt(252.0) * np.mean(values) / volatility)


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


def _candidate_identity_key(value: object) -> tuple[int, int | str]:
    text = str(value)
    try:
        return 0, int(text)
    except ValueError:
        return 1, text


def cscv_pbo(
    returns: pd.DataFrame, block_count: int = 10
) -> tuple[pd.DataFrame, dict[str, float | int]]:
    """Run combinatorially symmetric cross-validation on candidate returns."""
    if returns.empty or returns.shape[1] < 2:
        raise ValueError("CSCV requires at least two candidates and one row")
    matrix = returns.astype(float)
    if not np.isfinite(matrix.to_numpy()).all():
        raise ValueError("candidate returns must all be finite")
    blocks = contiguous_blocks(len(matrix), block_count)
    half = block_count // 2
    records: list[dict[str, object]] = []
    columns = list(map(str, matrix.columns))
    matrix.columns = columns
    candidate_count = len(columns)
    lower_clip = 0.5 / candidate_count
    upper_clip = 1.0 - lower_clip
    for split_id, training_blocks in enumerate(combinations(range(block_count), half)):
        training_set = set(training_blocks)
        validation_blocks = tuple(index for index in range(block_count) if index not in training_set)
        training_rows = np.concatenate([blocks[index] for index in training_blocks])
        validation_rows = np.concatenate([blocks[index] for index in validation_blocks])
        training_sharpes = candidate_sharpes(matrix.iloc[training_rows])
        if training_sharpes.notna().sum() < 2:
            raise ValueError(f"split {split_id} has fewer than two finite training sharpes")
        selected = str(
            sorted(
                training_sharpes.dropna().items(),
                key=lambda item: (-float(item[1]), _candidate_identity_key(item[0])),
            )[0][0]
        )
        validation_sharpes = candidate_sharpes(matrix.iloc[validation_rows])
        selected_validation = float(validation_sharpes.loc[selected])
        finite_validation = validation_sharpes.dropna().sort_values(ascending=False, kind="stable")
        if len(finite_validation) < 2 or selected not in finite_validation.index:
            raise ValueError(f"split {split_id} has invalid validation sharpes")
        rank = int(np.flatnonzero(finite_validation.index.to_numpy() == selected)[0]) + 1
        percentile = float((len(finite_validation) - rank) / (len(finite_validation) - 1))
        clipped = float(np.clip(percentile, lower_clip, upper_clip))
        records.append(
            {
                "split_id": split_id,
                "training_blocks": ",".join(map(str, training_blocks)),
                "validation_blocks": ",".join(map(str, validation_blocks)),
                "selected_candidate": selected,
                "training_sharpe": float(training_sharpes.loc[selected]),
                "validation_sharpe": selected_validation,
                "validation_rank": rank,
                "validation_candidate_count": int(len(finite_validation)),
                "validation_percentile": percentile,
                "logit": float(np.log(clipped / (1.0 - clipped))),
            }
        )
    details = pd.DataFrame.from_records(records)
    pbo = float(details["logit"].lt(0.0).mean())
    summary: dict[str, float | int] = {
        "block_count": int(block_count),
        "split_count": int(len(details)),
        "candidate_count": int(candidate_count),
        "pbo": pbo,
        "median_validation_percentile": float(details["validation_percentile"].median()),
        "median_logit": float(details["logit"].median()),
    }
    return details, summary


def deflated_sharpe_ratio(
    selected_returns: pd.Series, trial_sharpes: pd.Series
) -> dict[str, float | int]:
    """Compute the multiple-trial Deflated Sharpe probability."""
    values = selected_returns.astype(float).replace([np.inf, -np.inf], np.nan).dropna()
    trials = trial_sharpes.astype(float).replace([np.inf, -np.inf], np.nan).dropna()
    if len(values) < 3 or len(trials) < 2:
        raise ValueError("DSR requires at least three returns and two finite trial sharpes")
    trial_std = float(trials.std(ddof=1))
    if not np.isfinite(trial_std) or trial_std <= 0.0:
        raise ValueError("trial sharpes must have positive finite dispersion")
    euler_gamma = 0.5772156649015329
    trial_count = len(trials)
    expected_standard_max = (
        (1.0 - euler_gamma) * norm.ppf(1.0 - 1.0 / trial_count)
        + euler_gamma * norm.ppf(1.0 - 1.0 / (trial_count * np.e))
    )
    expected_max = float(trial_std * expected_standard_max)
    observed = annualized_sharpe(values.to_numpy())
    daily_observed = observed / np.sqrt(252.0)
    daily_benchmark = expected_max / np.sqrt(252.0)
    skew = float(values.skew())
    pearson_kurtosis = float(values.kurt() + 3.0)
    variance_adjustment = float(
        1.0
        - skew * daily_observed
        + ((pearson_kurtosis - 1.0) / 4.0) * daily_observed**2
    )
    if not np.isfinite(variance_adjustment) or variance_adjustment <= 0.0:
        raise ValueError("DSR variance adjustment must be positive and finite")
    statistic = float(
        (daily_observed - daily_benchmark)
        * np.sqrt(len(values) - 1.0)
        / np.sqrt(variance_adjustment)
    )
    return {
        "observations": int(len(values)),
        "trial_count": int(trial_count),
        "observed_sharpe": float(observed),
        "trial_sharpe_mean": float(trials.mean()),
        "trial_sharpe_std": trial_std,
        "expected_max_sharpe": expected_max,
        "skew": skew,
        "pearson_kurtosis": pearson_kurtosis,
        "test_statistic": statistic,
        "dsr_probability": float(norm.cdf(statistic)),
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
