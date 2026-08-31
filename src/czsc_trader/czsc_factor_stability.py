"""Causal stability diagnostics for categorical CZSC states and events."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd


ENDPOINTS = ("return_delta", "max_drawdown_delta")


def audit_causal_prefix(
    discovery: pd.DataFrame,
    replay: pd.DataFrame,
    factor_names: Sequence[str],
) -> dict[str, object]:
    """Require later-cutoff replay to preserve every earlier factor value."""
    names = list(map(str, factor_names))
    missing = sorted((set(names) - set(discovery.columns)) | (set(names) - set(replay.columns)))
    if missing:
        raise ValueError(f"causal prefix factors missing: {missing}")
    right = replay.reindex(discovery.index)
    mismatches: dict[str, int] = {}
    for name in names:
        left_values = discovery[name]
        right_values = right[name]
        equal = left_values.eq(right_values) | (left_values.isna() & right_values.isna())
        mismatches[name] = int((~equal).sum())
    total = int(sum(mismatches.values()))
    return {
        "status": "PASS" if total == 0 else "FAIL",
        "rows_checked": int(len(discovery)),
        "factors_checked": len(names),
        "mismatch_count": total,
        "mismatches_by_factor": mismatches,
    }


def leave_one_out_deltas(active_values: pd.Series, *, control_mean: float) -> list[float]:
    """Return active-minus-control means after omitting each active observation."""
    values = pd.to_numeric(active_values, errors="coerce").dropna().to_numpy(dtype=float)
    if len(values) <= 1:
        return []
    return [float(np.delete(values, offset).mean() - float(control_mean)) for offset in range(len(values))]


def _close_series(daily: pd.DataFrame, index: pd.DatetimeIndex) -> pd.Series:
    frame = daily.copy()
    if "dt" in frame.columns:
        frame = frame.set_index("dt")
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index), name="dt")
    close = pd.to_numeric(frame.sort_index()["close"], errors="raise").reindex(index)
    if close.isna().any() or close.le(0).any():
        raise ValueError("forward outcomes require positive close prices for every date")
    return close.astype(float)


def build_forward_outcomes(
    daily: pd.DataFrame,
    index: pd.DatetimeIndex,
    horizons: Sequence[int],
) -> pd.DataFrame:
    """Label each completed signal date using strictly subsequent sessions."""
    dates = pd.DatetimeIndex(pd.to_datetime(index), name="dt")
    if dates.has_duplicates or not dates.is_monotonic_increasing:
        raise ValueError("outcome index must be unique and increasing")
    close = _close_series(daily, dates)
    values = close.to_numpy(dtype=float)
    output: dict[str, np.ndarray] = {}
    for raw_horizon in horizons:
        horizon = int(raw_horizon)
        if horizon <= 0:
            raise ValueError("horizons must be positive")
        returns = np.full(len(values), np.nan)
        adverse = np.full(len(values), np.nan)
        drawdown = np.full(len(values), np.nan)
        for offset in range(len(values) - horizon):
            path = values[offset : offset + horizon + 1]
            returns[offset] = path[-1] / path[0] - 1.0
            adverse[offset] = np.min(path[1:] / path[0] - 1.0)
            peaks = np.maximum.accumulate(path)
            drawdown[offset] = np.min(path / peaks - 1.0)
        output[f"return_{horizon}"] = returns
        output[f"mae_{horizon}"] = adverse
        output[f"max_drawdown_{horizon}"] = drawdown
    return pd.DataFrame(output, index=dates)


def event_onsets(indicator: pd.Series) -> pd.Series:
    """Collapse a consecutive categorical event state to its first day."""
    active = indicator.fillna(0.0).astype(bool)
    return (active & ~active.shift(fill_value=False)).rename(indicator.name)


def evaluate_factor_stability(
    indicator: pd.Series,
    outcomes: pd.DataFrame,
    *,
    factor_kind: str,
    years: Sequence[int],
    horizons: Sequence[int],
) -> pd.DataFrame:
    """Compare active and inactive outcomes within each requested calendar year."""
    if factor_kind not in {"state", "event"}:
        raise ValueError("factor_kind must be state or event")
    values = indicator.reindex(outcomes.index)
    active = event_onsets(values) if factor_kind == "event" else values.fillna(0.0).astype(bool)
    factor_name = str(indicator.name or "unnamed")
    rows: list[dict[str, object]] = []
    for year in map(int, years):
        year_mask = outcomes.index.year == year
        for raw_horizon in horizons:
            horizon = int(raw_horizon)
            columns = [f"return_{horizon}", f"mae_{horizon}", f"max_drawdown_{horizon}"]
            valid = year_mask & outcomes[columns].notna().all(axis=1).to_numpy()
            active_mask = valid & active.to_numpy(dtype=bool)
            control_mask = valid & ~active.to_numpy(dtype=bool)
            active_values = outcomes.loc[active_mask, columns]
            control_values = outcomes.loc[control_mask, columns]
            active_means = active_values.mean()
            control_means = control_values.mean()
            rows.append(
                {
                    "factor": factor_name,
                    "factor_kind": factor_kind,
                    "year": year,
                    "horizon": horizon,
                    "active_n": int(active_mask.sum()),
                    "control_n": int(control_mask.sum()),
                    "active_return": float(active_means[columns[0]]),
                    "control_return": float(control_means[columns[0]]),
                    "return_delta": float(active_means[columns[0]] - control_means[columns[0]]),
                    "active_mae": float(active_means[columns[1]]),
                    "control_mae": float(control_means[columns[1]]),
                    "mae_delta": float(active_means[columns[1]] - control_means[columns[1]]),
                    "active_max_drawdown": float(active_means[columns[2]]),
                    "control_max_drawdown": float(control_means[columns[2]]),
                    "max_drawdown_delta": float(active_means[columns[2]] - control_means[columns[2]]),
                }
            )
    return pd.DataFrame(rows)


def _consistent_effect(values: pd.Series, minimum_absolute_effect: float) -> tuple[int, float] | None:
    numeric = pd.to_numeric(values, errors="coerce")
    if numeric.isna().any() or numeric.eq(0.0).any():
        return None
    signs = np.sign(numeric.to_numpy(dtype=float)).astype(int)
    if len(set(signs.tolist())) != 1:
        return None
    effect = float(numeric.mean())
    if abs(effect) + 1e-15 < float(minimum_absolute_effect):
        return None
    return int(signs[0]), effect


def select_discovery_candidates(
    metrics: pd.DataFrame,
    *,
    years: Sequence[int],
    primary_horizon: int,
    minimum_absolute_effect: float,
) -> pd.DataFrame:
    """Freeze one stable endpoint and direction per discovery factor."""
    required_years = tuple(map(int, years))
    primary = metrics[metrics["horizon"].eq(int(primary_horizon))]
    rows: list[dict[str, object]] = []
    for factor, group in primary.groupby("factor", sort=True):
        indexed = group.drop_duplicates("year").set_index("year")
        if not set(required_years) <= set(map(int, indexed.index)):
            continue
        ordered = indexed.loc[list(required_years)]
        choices: list[tuple[float, int, str, int, float]] = []
        for endpoint_rank, endpoint in enumerate(ENDPOINTS):
            if endpoint not in ordered:
                continue
            result = _consistent_effect(ordered[endpoint], minimum_absolute_effect)
            if result is None:
                continue
            direction, effect = result
            choices.append((abs(effect), -endpoint_rank, endpoint, direction, effect))
        if not choices:
            continue
        _, _, endpoint, direction, effect = max(choices)
        rows.append(
            {
                "factor": str(factor),
                "factor_kind": str(ordered.iloc[0]["factor_kind"]),
                "endpoint": endpoint,
                "direction": int(direction),
                "discovery_effect": float(effect),
            }
        )
    return pd.DataFrame(
        rows,
        columns=["factor", "factor_kind", "endpoint", "direction", "discovery_effect"],
    ).sort_values("factor", kind="stable", ignore_index=True)


def validate_frozen_candidates(
    metrics: pd.DataFrame,
    candidates: pd.DataFrame,
    *,
    years: Sequence[int],
    primary_horizon: int,
    minimum_absolute_effect: float,
) -> pd.DataFrame:
    """Apply discovery-frozen endpoint and direction without reselection."""
    required_years = tuple(map(int, years))
    primary = metrics[metrics["horizon"].eq(int(primary_horizon))]
    rows: list[dict[str, object]] = []
    for candidate in candidates.itertuples(index=False):
        group = primary[primary["factor"].eq(candidate.factor)]
        indexed = group.drop_duplicates("year").set_index("year")
        passed = False
        effect = float("nan")
        yearly_effects: dict[str, float] = {}
        if set(required_years) <= set(map(int, indexed.index)) and candidate.endpoint in indexed:
            ordered = pd.to_numeric(indexed.loc[list(required_years), candidate.endpoint], errors="coerce")
            yearly_effects = {str(year): float(value) for year, value in ordered.items()}
            if not ordered.isna().any():
                effect = float(ordered.mean())
                passed = bool(
                    (ordered * int(candidate.direction) > 0.0).all()
                    and abs(effect) + 1e-15 >= float(minimum_absolute_effect)
                )
        rows.append(
            {
                **candidate._asdict(),
                "validation_effect": effect,
                "validation_yearly_effects": yearly_effects,
                "pass": passed,
            }
        )
    return pd.DataFrame(rows)
