"""Causal event census for pre-close and opening-gap ETF mechanisms."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from .intraday_medium_frequency import _rolling_density
from .intraday_opportunity_map import _price_checkpoints


@dataclass(frozen=True)
class EarlySessionEventCensus:
    """Causal daily features, events and rolling density evidence."""

    features: pd.DataFrame
    events: pd.DataFrame
    density: pd.DataFrame


def census_early_session_events(
    five_minute: pd.DataFrame,
    mechanisms: Sequence[Mapping[str, str]],
    *,
    evaluation_start: pd.Timestamp | str,
    threshold_lookback_sessions: int,
    threshold_quantile: float,
    threshold_lag_sessions: int,
    density_window_sessions: int,
    target_median_min: int,
    target_median_max: int,
    median_events_required: int,
    p10_events_required: int,
) -> EarlySessionEventCensus:
    """Create fixed events without reading prices after each information clock."""

    if threshold_lookback_sessions <= 0 or threshold_lag_sessions < 1:
        raise ValueError("threshold history and lag must be positive")
    if not 0 < threshold_quantile < 1:
        raise ValueError("threshold quantile must be between zero and one")
    checkpoints = _price_checkpoints(five_minute)
    features = pd.DataFrame(index=checkpoints.index)
    features.index.name = "trade_date"
    features["previous_trade_date"] = pd.Series(
        checkpoints.index, index=checkpoints.index
    ).shift(1)
    current_intraday = checkpoints["15:00_CLOSE"].div(checkpoints["OPEN"]).sub(1.0)
    current_late = checkpoints["15:00_CLOSE"].div(checkpoints["13:05_OPEN"]).sub(1.0)
    features["previous_intraday_return"] = current_intraday.shift(1)
    features["previous_late_session_return"] = current_late.shift(1)
    features["opening_gap_return"] = checkpoints["OPEN"].div(
        checkpoints["PREVIOUS_CLOSE"]
    ).sub(1.0)

    definitions = {
        "PRIOR_INTRADAY_CONTINUATION": ("previous_intraday_return", 1),
        "LATE_SESSION_FLOW_CONTINUATION": ("previous_late_session_return", 1),
        "OPENING_GAP_REVERSION": ("opening_gap_return", -1),
    }
    mechanism_ids = [str(item["mechanism_id"]) for item in mechanisms]
    if set(mechanism_ids) != set(definitions) or len(mechanism_ids) != len(definitions):
        raise ValueError("mechanisms differ from the supported frozen definitions")
    metadata = {str(item["mechanism_id"]): item for item in mechanisms}
    event_frames: list[pd.DataFrame] = []
    for mechanism_id in mechanism_ids:
        feature_name, direction_multiplier = definitions[mechanism_id]
        value = features[feature_name]
        strength = value.abs()
        threshold = (
            strength.shift(threshold_lag_sessions)
            .rolling(
                threshold_lookback_sessions,
                min_periods=threshold_lookback_sessions,
            )
            .quantile(threshold_quantile)
        )
        event = threshold.notna() & value.ne(0) & strength.ge(threshold)
        features[f"{mechanism_id}_strength"] = strength
        features[f"{mechanism_id}_threshold"] = threshold
        features[f"{mechanism_id}_event"] = event
        selected = features.loc[event].copy().reset_index()
        selected.insert(0, "mechanism_id", mechanism_id)
        selected["direction"] = np.sign(selected[feature_name]).astype(int) * direction_multiplier
        selected["information_clock"] = str(metadata[mechanism_id]["information_clock"])
        selected["planned_entry"] = str(metadata[mechanism_id]["planned_entry"])
        selected["planned_exit"] = str(metadata[mechanism_id]["planned_exit"])
        event_frames.append(selected)

    start = pd.Timestamp(evaluation_start).normalize()
    features = features.loc[features.index >= start].reset_index()
    events = pd.concat(event_frames, ignore_index=True)
    events = events.loc[events["trade_date"] >= start].reset_index(drop=True)
    if events.duplicated(["mechanism_id", "trade_date"]).any():
        raise ValueError("a mechanism generated more than one event per day")
    calendar = pd.DatetimeIndex(features["trade_date"], name="trade_date")
    rows: list[dict[str, object]] = []
    for mechanism_id in mechanism_ids:
        selected = events.loc[events["mechanism_id"].eq(mechanism_id)]
        median_count, p10_count, minimum_count, maximum_count = _rolling_density(
            pd.DatetimeIndex(selected["trade_date"]),
            calendar,
            density_window_sessions,
        )
        capable = bool(
            median_count >= median_events_required
            and p10_count >= p10_events_required
        )
        rows.append(
            {
                "mechanism_id": mechanism_id,
                "events": int(len(selected)),
                "long_events": int(selected["direction"].eq(1).sum()),
                "short_overlay_events": int(selected["direction"].eq(-1).sum()),
                "rolling_window_sessions": int(density_window_sessions),
                "rolling_median_events": median_count,
                "rolling_p10_events": p10_count,
                "rolling_min_events": minimum_count,
                "rolling_max_events": maximum_count,
                "inside_target_band": bool(
                    target_median_min <= median_count <= target_median_max
                ),
                "density_pass": capable,
                "evidence": "DENSITY_CAPABLE" if capable else "EVIDENCE_RATE_FAIL",
            }
        )
    return EarlySessionEventCensus(features, events, pd.DataFrame(rows))
