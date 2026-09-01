"""Statistics for preregistered EX13 score-tier monotonicity diagnosis."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .czsc_multiplicity import hac_incremental_test
from .score_tier_position import classify_score_tiers


@dataclass(frozen=True)
class ScoreTierDiagnostic:
    summary: dict[str, object]
    tier_metrics: pd.DataFrame
    yearly_metrics: pd.DataFrame
    hac_result: dict[str, float | int]
    influence_audit: pd.DataFrame


def _bootstrap_interval(values: pd.Series, *, seed: int) -> tuple[float, float]:
    clean = pd.to_numeric(values, errors="coerce").dropna().to_numpy(dtype=float)
    if len(clean) < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = np.mean(rng.choice(clean, size=(500, len(clean)), replace=True), axis=1)
    low, high = np.quantile(means, [0.025, 0.975])
    return float(low), float(high)


def _tier_metrics(
    tiers: pd.Series,
    target: pd.Series,
    outcomes: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for scope, scope_mask in (
        ("all", pd.Series(True, index=tiers.index)),
        ("held", target.eq(1.0)),
    ):
        for tier in range(5):
            mask = scope_mask & tiers.eq(tier)
            row: dict[str, object] = {
                "scope": scope,
                "tier": tier,
                "count": int(mask.sum()),
            }
            for horizon in (5, 10, 20):
                returns = outcomes.loc[mask, f"return_{horizon}"]
                drawdowns = outcomes.loc[mask, f"max_drawdown_{horizon}"]
                row[f"return_{horizon}"] = float(returns.mean())
                row[f"max_drawdown_{horizon}"] = float(drawdowns.mean())
                row[f"win_rate_{horizon}"] = float(returns.gt(0.0).mean())
            return_low, return_high = _bootstrap_interval(
                outcomes.loc[mask, "return_20"], seed=100 + tier + (10 if scope == "held" else 0)
            )
            drawdown_low, drawdown_high = _bootstrap_interval(
                outcomes.loc[mask, "max_drawdown_20"], seed=200 + tier + (10 if scope == "held" else 0)
            )
            row.update(
                {
                    "return_20_ci_low": return_low,
                    "return_20_ci_high": return_high,
                    "max_drawdown_20_ci_low": drawdown_low,
                    "max_drawdown_20_ci_high": drawdown_high,
                }
            )
            rows.append(row)
    return pd.DataFrame(rows)


def _contrast(
    mask: np.ndarray,
    tiers: pd.Series,
    outcomes: pd.DataFrame,
    *,
    minimum_support: int,
) -> dict[str, object]:
    high = mask & tiers.eq(4).to_numpy()
    low = mask & tiers.isin([1, 2]).to_numpy()
    high_count = int(high.sum())
    low_count = int(low.sum())
    supported = high_count >= minimum_support and low_count >= minimum_support
    return {
        "high_count": high_count,
        "low_count": low_count,
        "supported": supported,
        "return_effect": (
            float(outcomes.loc[high, "return_20"].mean() - outcomes.loc[low, "return_20"].mean())
            if supported
            else float("nan")
        ),
        "drawdown_effect": (
            float(
                outcomes.loc[high, "max_drawdown_20"].mean()
                - outcomes.loc[low, "max_drawdown_20"].mean()
            )
            if supported
            else float("nan")
        ),
    }


def diagnose_score_monotonicity(
    scores: pd.Series,
    target: pd.Series,
    event: pd.Series,
    outcomes: pd.DataFrame,
    *,
    minimum_annual_support: int = 30,
    hac_max_lag: int = 20,
) -> ScoreTierDiagnostic:
    """Apply the frozen cross-year, HAC, and influence gates."""
    index = pd.DatetimeIndex(pd.to_datetime(scores.index), name="dt")
    if not scores.index.equals(target.index) or not scores.index.equals(event.index):
        raise ValueError("score, target, and event indices differ")
    if not index.equals(outcomes.index):
        raise ValueError("outcome index differs from score index")
    if set(index.year) - set(range(2021, 2026)):
        raise ValueError("diagnostic accepts only 2021-2025 observations")
    tiers = classify_score_tiers(scores)
    target = target.astype(float)
    event = event.astype(float)
    held = target.eq(1.0).to_numpy()
    valid = outcomes[["return_20", "max_drawdown_20"]].notna().all(axis=1).to_numpy()
    yearly_rows: list[dict[str, object]] = []
    for year in range(2021, 2026):
        year_mask = (index.year == year) & held & valid
        yearly_rows.append({"year": year, **_contrast(year_mask, tiers, outcomes, minimum_support=minimum_annual_support)})
    yearly = pd.DataFrame(yearly_rows)
    supported = yearly[yearly["supported"]].copy()
    same_direction = int(
        ((supported["return_effect"] >= 0.0) & (supported["drawdown_effect"] > 0.0)).sum()
    )
    aggregate = _contrast(held & valid, tiers, outcomes, minimum_support=minimum_annual_support)
    absolute_sum = float(supported["drawdown_effect"].abs().sum())
    maximum_share = (
        float(supported["drawdown_effect"].abs().max() / absolute_sum)
        if absolute_sum > 0.0 and not supported.empty
        else float("inf")
    )
    influence_rows: list[dict[str, object]] = []
    for omitted_year in range(2021, 2026):
        mask = held & valid & (index.year != omitted_year)
        contrast = _contrast(mask, tiers, outcomes, minimum_support=minimum_annual_support)
        influence_rows.append(
            {
                "omitted_year": omitted_year,
                "leave_one_year_out_return_effect": contrast["return_effect"],
                "leave_one_year_out_drawdown_effect": contrast["drawdown_effect"],
            }
        )
    influence = pd.DataFrame(influence_rows)
    hac_mask = valid
    hac = hac_incremental_test(
        outcomes.loc[hac_mask, "max_drawdown_20"].to_numpy(dtype=float),
        tiers.loc[hac_mask].to_numpy(dtype=float),
        np.column_stack(
            [
                target.loc[hac_mask].to_numpy(dtype=float),
                event.loc[hac_mask].to_numpy(dtype=float),
            ]
        ),
        max_lag=hac_max_lag,
    )
    pass_gate = bool(
        len(supported) == 5
        and same_direction >= 4
        and float(aggregate["return_effect"]) >= 0.0
        and float(aggregate["drawdown_effect"]) > 0.0
        and float(hac["coefficient"]) > 0.0
        and float(hac["p_value"]) <= 0.10
        and maximum_share <= 0.50
        and influence["leave_one_year_out_drawdown_effect"].gt(0.0).all()
    )
    summary = {
        "status": "PASS" if pass_gate else "FAIL",
        "supported_years": int(len(supported)),
        "same_direction_years": same_direction,
        "aggregate_return_effect": float(aggregate["return_effect"]),
        "aggregate_drawdown_effect": float(aggregate["drawdown_effect"]),
        "maximum_annual_absolute_effect_share": maximum_share,
        "hac_coefficient": float(hac["coefficient"]),
        "hac_p_value": float(hac["p_value"]),
    }
    return ScoreTierDiagnostic(
        summary=summary,
        tier_metrics=_tier_metrics(tiers, target, outcomes),
        yearly_metrics=yearly,
        hac_result=hac,
        influence_audit=influence,
    )
