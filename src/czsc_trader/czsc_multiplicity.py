"""Multiple-testing controls for the terminal CZSC research route."""

from __future__ import annotations

from math import erfc, sqrt

import numpy as np


def benjamini_hochberg(p_values: np.ndarray) -> np.ndarray:
    """Return Benjamini-Hochberg adjusted q-values in original order."""
    values = np.asarray(p_values, dtype=float)
    if values.ndim != 1 or np.isnan(values).any() or ((values < 0) | (values > 1)).any():
        raise ValueError("p-values must be a finite one-dimensional array in [0, 1]")
    count = len(values)
    if count == 0:
        return values.copy()
    order = np.argsort(values, kind="stable")
    ranked = values[order]
    adjusted = ranked * count / np.arange(1, count + 1)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    output = np.empty(count, dtype=float)
    output[order] = np.clip(adjusted, 0.0, 1.0)
    return output


def hac_incremental_test(
    outcome: np.ndarray,
    candidate: np.ndarray,
    references: np.ndarray,
    *,
    max_lag: int,
) -> dict[str, float | int]:
    """OLS candidate coefficient with a Bartlett Newey-West covariance."""
    y = np.asarray(outcome, dtype=float).reshape(-1)
    c = np.asarray(candidate, dtype=float).reshape(-1)
    r = np.asarray(references, dtype=float)
    if r.ndim == 1:
        r = r.reshape(-1, 1)
    if len(y) != len(c) or len(y) != len(r):
        raise ValueError("HAC inputs must have equal row counts")
    if int(max_lag) < 0:
        raise ValueError("max_lag must be non-negative")
    valid = np.isfinite(y) & np.isfinite(c) & np.isfinite(r).all(axis=1)
    y = y[valid]
    c = c[valid]
    r = r[valid]
    if len(y) < 3 or np.ptp(c) == 0.0:
        return {"coefficient": 0.0, "standard_error": float("inf"), "p_value": 1.0, "nobs": int(len(y)), "rank": 0}
    if r.size:
        r = r[:, np.ptp(r, axis=0) > 0.0]
    x = np.column_stack([np.ones(len(y)), c, r])
    rank = int(np.linalg.matrix_rank(x))
    xtx_inverse = np.linalg.pinv(x.T @ x)
    beta = xtx_inverse @ x.T @ y
    residual = y - x @ beta
    scores = x * residual[:, None]
    meat = scores.T @ scores
    lag_limit = min(int(max_lag), len(y) - 1)
    for lag in range(1, lag_limit + 1):
        weight = 1.0 - lag / (lag_limit + 1.0)
        cross = scores[lag:].T @ scores[:-lag]
        meat += weight * (cross + cross.T)
    covariance = xtx_inverse @ meat @ xtx_inverse
    variance = max(float(covariance[1, 1]), 0.0)
    standard_error = sqrt(variance)
    coefficient = float(beta[1])
    if standard_error == 0.0:
        p_value = 0.0 if coefficient != 0.0 else 1.0
    else:
        p_value = float(erfc(abs(coefficient / standard_error) / sqrt(2.0)))
    return {
        "coefficient": coefficient,
        "standard_error": standard_error,
        "p_value": p_value,
        "nobs": int(len(y)),
        "rank": rank,
    }
