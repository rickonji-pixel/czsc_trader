"""Entry-fixed fractional sizing for frozen four-layer scores."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .rules import Rule


EVENT_COLUMNS = (
    "event_id",
    "signal_date",
    "event_type",
    "factor_score",
    "enter_threshold",
    "full_threshold",
    "exit_threshold",
    "before_position",
    "after_position",
    "reason",
)


def positions_from_entry_sizing(
    scores: pd.Series,
    enter: float,
    full: float,
    exit_: float,
    state_rule: Rule,
    *,
    partial_position: float = 0.5,
) -> pd.Series:
    """Choose half/full exposure at entry and keep it unchanged until exit."""
    enter = float(enter)
    full = float(full)
    exit_ = float(exit_)
    partial_position = float(partial_position)
    if not np.isfinite([enter, full, exit_, partial_position]).all():
        raise ValueError("position-sizing thresholds and positions must be finite")
    if not exit_ < enter < full:
        raise ValueError("position-sizing thresholds must satisfy exit < enter < full")
    if not 0.0 < partial_position < 1.0:
        raise ValueError("partial_position must be strictly between 0 and 1")
    if state_rule.entry_gate != "none":
        raise ValueError("entry-fixed sizing requires baseline entry_gate none")
    values = scores.astype(float)
    if values.isna().any() or not np.isfinite(values).all():
        raise ValueError("position-sizing scores must be finite")
    if not values.index.is_monotonic_increasing or values.index.has_duplicates:
        raise ValueError("position-sizing score index must be unique and increasing")

    position = 0.0
    confirmations = 0
    exit_confirmations = 0
    holding_days = 0
    output: list[float] = []
    for score in values:
        if position == 0.0:
            confirmations = confirmations + 1 if score >= enter else 0
            if confirmations >= state_rule.confirm_days:
                position = 1.0 if score >= full else partial_position
                holding_days = 1
                confirmations = 0
                exit_confirmations = 0
        else:
            eligible = holding_days >= state_rule.min_hold_days
            exit_confirmations = (
                exit_confirmations + 1 if eligible and score <= exit_ else 0
            )
            if exit_confirmations >= state_rule.exit_confirm_days:
                position = 0.0
                holding_days = 0
                confirmations = 0
                exit_confirmations = 0
            else:
                holding_days += 1
        output.append(position)
    return pd.Series(output, index=values.index, name="target_position", dtype=float)


def build_entry_sizing_events(
    target_position: pd.Series,
    scores: pd.Series,
    enter: float,
    full: float,
    exit_: float,
) -> pd.DataFrame:
    """Describe every entry-fixed position transition for causal audit."""
    if not target_position.index.equals(scores.index):
        raise ValueError("entry-sizing target and score indices must match")
    target = target_position.astype(float)
    if target.isna().any() or not target.isin([0.0, 0.5, 1.0]).all():
        raise ValueError("entry-sizing target must contain only 0, 0.5, or 1")
    previous = target.shift(1, fill_value=0.0)
    rows: list[dict[str, object]] = []
    for signal_date in target.index[target.ne(previous)]:
        before = float(previous.loc[signal_date])
        after = float(target.loc[signal_date])
        if before == 0.0 and after > 0.0:
            event_type = "Entry"
        elif before > 0.0 and after == 0.0:
            event_type = "Exit"
        else:
            raise ValueError("entry-fixed sizing cannot resize an open position")
        score = float(scores.loc[signal_date])
        if event_type == "Entry":
            reason = (
                f"score {score:.6f} >= full {float(full):.6f}"
                if after == 1.0
                else (
                    f"enter {float(enter):.6f} <= score {score:.6f} "
                    f"< full {float(full):.6f}"
                )
            )
        else:
            reason = f"score {score:.6f} <= exit {float(exit_):.6f}"
        rows.append(
            {
                "event_id": (
                    f"EntrySizing:{pd.Timestamp(signal_date):%Y%m%d}:{event_type}"
                ),
                "signal_date": pd.Timestamp(signal_date),
                "event_type": event_type,
                "factor_score": score,
                "enter_threshold": float(enter),
                "full_threshold": float(full),
                "exit_threshold": float(exit_),
                "before_position": before,
                "after_position": after,
                "reason": reason,
            }
        )
    return pd.DataFrame(rows, columns=EVENT_COLUMNS)
