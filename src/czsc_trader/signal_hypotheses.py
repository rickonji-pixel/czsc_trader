"""Auditable reduction of signal-census evidence into research hypotheses."""

from __future__ import annotations

from typing import Mapping, Sequence

import numpy as np
import pandas as pd
import plotly.graph_objects as go


def _duplicate_map(redundancy: Mapping[str, object]) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    for index, group in enumerate(redundancy["duplicate_groups"], start=1):
        members = sorted(
            group["members"],
            key=lambda item: (item["frequency"], item["name"], item["state_id"]),
        )
        canonical = str(members[0]["state_id"])
        for member in members:
            result[str(member["state_id"])] = (index, canonical)
    return result


def build_review_universe(
    metrics: pd.DataFrame,
    redundancy: Mapping[str, object],
    review: Mapping[str, object],
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Apply only frozen mechanical rules and exact-behavior deduplication."""
    horizons = {int(value) for value in review["horizons"]}
    scoped = metrics.loc[
        metrics["horizon"].isin(horizons)
        & metrics["evidence_quality"].eq(review["required_evidence_quality"])
        & ~metrics["state_primary"].isin(review["excluded_primary_states"])
    ].copy()
    duplicate_lookup = _duplicate_map(redundancy)
    rows = []
    before_deduplication = 0
    for state_id, group in scoped.groupby("state_id", sort=True):
        if set(group["horizon"].astype(int)) != horizons:
            continue
        means = group.set_index("horizon")["net_return_mean"].astype(float)
        same_direction = means.gt(0).all() or means.lt(0).all()
        minimum_consistency = float(group["year_sign_consistency"].min())
        if bool(review["require_same_mean_direction"]) and not same_direction:
            continue
        if minimum_consistency < float(review["minimum_year_sign_consistency"]):
            continue
        before_deduplication += 1
        duplicate_group, canonical = duplicate_lookup.get(str(state_id), (0, str(state_id)))
        if bool(review["collapse_exact_behavior_duplicates"]) and canonical != state_id:
            continue
        first = group.iloc[0]
        by_horizon = group.set_index("horizon")
        rows.append({
            "state_id": state_id,
            "frequency": first["frequency"],
            "name": first["name"],
            "namespace": first["namespace"],
            "state_primary": first["state_primary"],
            "forward_bias": "POSITIVE" if means.gt(0).all() else "NEGATIVE",
            "minimum_event_count": int(group["event_count"].min()),
            "coverage_years": int(group["coverage_years"].min()),
            "minimum_year_sign_consistency": minimum_consistency,
            "median_absolute_standardized_effect": float(
                group["standardized_effect"].abs().median()
            ),
            "net_return_h3": float(by_horizon.at[3, "net_return_mean"]),
            "net_return_h5": float(by_horizon.at[5, "net_return_mean"]),
            "net_return_h10": float(by_horizon.at[10, "net_return_mean"]),
            "mfe_h5": float(by_horizon.at[5, "mfe_mean"]),
            "mae_h5": float(by_horizon.at[5, "mae_mean"]),
            "exact_duplicate_group": duplicate_group,
        })
    result = pd.DataFrame(rows).sort_values(
        [
            "minimum_year_sign_consistency",
            "median_absolute_standardized_effect",
            "minimum_event_count",
        ],
        ascending=[False, False, False],
    ).reset_index(drop=True)
    result.insert(0, "mechanical_rank", np.arange(1, len(result) + 1))
    return result, {
        "eligible_before_exact_deduplication": before_deduplication,
        "eligible_after_exact_deduplication": len(result),
    }


def resolve_semantic_reviews(
    reviews: Sequence[Mapping[str, object]],
    metrics: pd.DataFrame,
    universe: pd.DataFrame,
) -> pd.DataFrame:
    """Resolve human-readable review keys and guard every selected hypothesis."""
    rows = []
    eligible_ids = set(universe["state_id"])
    for review in reviews:
        matched = metrics.loc[
            metrics["frequency"].eq(review["frequency"])
            & metrics["name"].eq(review["name"])
            & metrics["state_primary"].eq(review["state"])
        ].copy()
        state_ids = matched["state_id"].drop_duplicates().tolist()
        if len(state_ids) != 1:
            raise ValueError(f"semantic review does not resolve uniquely: {review}")
        state_id = str(state_ids[0])
        by_horizon = matched.set_index("horizon")
        rows.append({
            "state_id": state_id,
            "frequency": review["frequency"],
            "name": review["name"],
            "state_primary": review["state"],
            "decision": review["decision"],
            "role": review["role"],
            "hypothesis": review.get("hypothesis", ""),
            "mechanical_eligible": state_id in eligible_ids,
            "event_count_h5": int(by_horizon.at[5, "event_count"]),
            "coverage_years_h5": int(by_horizon.at[5, "coverage_years"]),
            "year_sign_consistency_h3": float(
                by_horizon.at[3, "year_sign_consistency"]
            ),
            "year_sign_consistency_h5": float(
                by_horizon.at[5, "year_sign_consistency"]
            ),
            "year_sign_consistency_h10": float(
                by_horizon.at[10, "year_sign_consistency"]
            ),
            "net_return_h3": float(by_horizon.at[3, "net_return_mean"]),
            "net_return_h5": float(by_horizon.at[5, "net_return_mean"]),
            "net_return_h10": float(by_horizon.at[10, "net_return_mean"]),
            "mfe_h5": float(by_horizon.at[5, "mfe_mean"]),
            "mae_h5": float(by_horizon.at[5, "mae_mean"]),
            "reason": review["reason"],
        })
    result = pd.DataFrame(rows)
    selected = result.loc[result["decision"] == "SELECT"]
    if selected.empty or not selected["mechanical_eligible"].all():
        raise ValueError(
            "every selected hypothesis must belong to the frozen mechanical review universe"
        )
    return result


def event_overlap(
    selected_events: pd.DataFrame,
    trading_dates: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Measure exact and nearby trigger overlap without interpreting performance."""
    position = {session: index for index, session in enumerate(trading_dates)}
    event_sets = {
        state_id: {
            position[session]
            for session in pd.to_datetime(group["signal_date"]).dt.normalize()
            if session in position
        }
        for state_id, group in selected_events.groupby("state_id")
    }
    rows = []
    ids = sorted(event_sets)
    for left_index, left in enumerate(ids):
        for right in ids[left_index + 1 :]:
            left_events = event_sets[left]
            right_events = event_sets[right]
            rows.append({
                "left_state_id": left,
                "right_state_id": right,
                "left_events": len(left_events),
                "right_events": len(right_events),
                "same_day_events": len(left_events & right_events),
                "left_within_5_sessions_of_right": sum(
                    any(abs(value - other) <= 5 for other in right_events)
                    for value in left_events
                ),
                "right_within_5_sessions_of_left": sum(
                    any(abs(value - other) <= 5 for other in left_events)
                    for value in right_events
                ),
            })
    return pd.DataFrame(rows)


def write_review_chart(
    daily: pd.DataFrame,
    selected_events: pd.DataFrame,
    reviews: pd.DataFrame,
    path: str,
    *,
    start: str,
    end: str,
    title: str,
) -> None:
    """Write a self-contained chart for visual event-location review."""
    prices = daily.copy()
    prices["dt"] = pd.to_datetime(prices["dt"]).dt.normalize()
    prices = prices.loc[prices["dt"].between(pd.Timestamp(start), pd.Timestamp(end))].set_index("dt")
    figure = go.Figure(
        go.Candlestick(
            x=prices.index,
            open=prices["open"],
            high=prices["high"],
            low=prices["low"],
            close=prices["close"],
            name="日K",
            increasing_line_color="#ef4444",
            decreasing_line_color="#22c55e",
        )
    )
    palette = ["#60a5fa", "#a78bfa", "#fb7185", "#f59e0b"]
    selected = reviews.loc[reviews["decision"] == "SELECT"]
    for color, (_, review) in zip(palette, selected.iterrows()):
        events = selected_events.loc[
            selected_events["state_id"] == review["state_id"]
        ].copy()
        sessions = pd.to_datetime(events["signal_date"]).dt.normalize()
        events = events.loc[sessions.isin(prices.index)].copy()
        sessions = pd.to_datetime(events["signal_date"]).dt.normalize()
        risk = review["role"] == "RISK_FILTER"
        y = (
            prices.loc[sessions, "high"].to_numpy() * 1.025
            if risk
            else prices.loc[sessions, "low"].to_numpy() * 0.975
        )
        figure.add_trace(
            go.Scatter(
                x=sessions,
                y=y,
                mode="markers",
                marker={
                    "symbol": "triangle-down" if risk else "triangle-up",
                    "size": 9,
                    "color": color,
                },
                name=f"{review['role']} · {review['state_primary']}",
                customdata=np.column_stack(
                    [
                        events["name"].astype(str),
                        events["state_full"].fillna("").astype(str),
                        np.repeat(review["hypothesis"], len(events)),
                    ]
                ),
                hovertemplate=(
                    "%{x|%Y-%m-%d}<br>%{customdata[0]}<br>状态 %{customdata[1]}"
                    "<br>假设 %{customdata[2]}<extra></extra>"
                ),
            )
        )
    figure.update_layout(
        template="plotly_dark",
        title=title,
        height=760,
        margin={"l": 60, "r": 30, "t": 80, "b": 45},
        hovermode="x unified",
        xaxis_rangeslider_visible=False,
        legend={"orientation": "h", "y": 1.02, "x": 0},
    )
    figure.write_html(
        path,
        include_plotlyjs=True,
        full_html=True,
        config={"responsive": True, "displaylogo": False},
        div_id="signal-hypothesis-review",
    )
