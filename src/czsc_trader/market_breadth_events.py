"""Point-in-time index breadth features and return-free event census."""

from __future__ import annotations

import numpy as np
import pandas as pd


MECHANISM = "BREADTH_THRUST_CONTINUATION"


def build_market_breadth_features(panel: pd.DataFrame) -> pd.DataFrame:
    """Aggregate observed constituent directions without using future data."""

    required = {"dt", "con_code", "record_status", "pct_chg"}
    if missing := sorted(required.difference(panel.columns)):
        raise ValueError(f"constituent panel missing columns {missing}")
    frame = panel.copy()
    frame["dt"] = pd.to_datetime(frame["dt"]).dt.normalize()
    if frame.duplicated(["dt", "con_code"]).any():
        raise ValueError("constituent panel contains duplicate member-session keys")
    allowed = {"OBSERVED", "SUSPENDED", "DELISTED_STALE_MEMBERSHIP"}
    if unexpected := sorted(set(frame["record_status"]) - allowed):
        raise ValueError(f"unexpected constituent statuses {unexpected}")

    observed = frame["record_status"].eq("OBSERVED")
    returns = pd.to_numeric(frame["pct_chg"], errors="coerce")
    if not np.isfinite(returns.loc[observed].to_numpy(dtype=float)).all():
        raise ValueError("observed constituent percentage changes must be finite")
    frame["advance"] = observed & returns.gt(0)
    frame["decline"] = observed & returns.lt(0)
    frame["unchanged"] = observed & returns.eq(0)
    frame["observed"] = observed
    grouped = frame.groupby("dt", sort=True)
    daily = grouped[["advance", "decline", "unchanged", "observed"]].sum().rename(
        columns={
            "advance": "advancers",
            "decline": "decliners",
            "unchanged": "unchanged",
            "observed": "observed_members",
        }
    )
    daily["membership_rows"] = grouped.size()
    daily["observed_ratio"] = daily["observed_members"] / daily["membership_rows"]
    daily["breadth_balance"] = (
        (daily["advancers"] - daily["decliners"]) / daily["observed_members"]
    )
    daily["advance_ratio"] = daily["advancers"] / daily["observed_members"]
    daily["breadth_thrust_5"] = daily["breadth_balance"].rolling(5, min_periods=5).mean()
    return daily.reset_index()


def generate_breadth_thrust_events(
    features: pd.DataFrame,
    *,
    evaluation_start: str | pd.Timestamp,
    threshold_lookback: int = 60,
    upper_quantile: float = 0.75,
    minimum_observed_ratio: float = 0.95,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Generate broad-participation continuation events without reading returns."""

    frame = features.copy()
    frame["dt"] = pd.to_datetime(frame["dt"]).dt.normalize()
    frame = frame.sort_values("dt").reset_index(drop=True)
    score = frame["breadth_thrust_5"].astype(float)
    threshold = score.shift(1).rolling(
        threshold_lookback, min_periods=threshold_lookback
    ).quantile(upper_quantile)
    active = (
        score.gt(threshold) & frame["observed_ratio"].ge(minimum_observed_ratio)
    ).fillna(False)
    calendar = pd.DatetimeIndex(frame["dt"])
    next_session = pd.Series(calendar, index=calendar).shift(-1)
    signal_dates = calendar[active]
    events = pd.DataFrame(
        {
            "mechanism": MECHANISM,
            "signal_date": signal_dates,
            "event_date": next_session.reindex(signal_dates).to_numpy(),
            "signal_clock": "POST_CLOSE",
            "execution_clock": "NEXT_OPEN",
            "breadth_thrust_5": score.loc[active].to_numpy(),
            "prior_upper_quartile": threshold.loc[active].to_numpy(),
            "observed_ratio": frame.loc[active, "observed_ratio"].to_numpy(),
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
    evidence = frame.copy()
    evidence["prior_upper_quartile"] = threshold
    evidence["active"] = active
    return events, density, evidence
