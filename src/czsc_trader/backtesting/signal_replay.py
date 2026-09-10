from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256

import pandas as pd

from czsc_trader.strategy_runtime import apply_resolved_strategy

from .datasets import ReplayData
from .models import StrategySnapshot


@dataclass(frozen=True)
class SignalReplay:
    snapshot: StrategySnapshot
    decisions: pd.DataFrame
    calculation_start: pd.Timestamp
    calculation_end: pd.Timestamp
    evaluation_start: pd.Timestamp
    evaluation_end: pd.Timestamp


def _decision_id(reference: str, signal_date: pd.Timestamp, target: int) -> str:
    raw = f"{reference}|{signal_date.date()}|{target}".encode()
    return "DEC-" + sha256(raw).hexdigest()[:20].upper()


def replay_signals(
    snapshot: StrategySnapshot,
    replay_data: ReplayData,
    start: date,
    end: date,
) -> SignalReplay:
    """Generate causal daily decisions from the complete calculation context."""
    requested_start = pd.Timestamp(start).normalize()
    requested_end = pd.Timestamp(end).normalize()
    if requested_start > requested_end:
        raise ValueError("backtest start must not be after end")
    daily = replay_data.adjusted.daily
    sessions = pd.DatetimeIndex(pd.to_datetime(daily["dt"]), name="dt")
    evaluation = sessions[(sessions >= requested_start) & (sessions <= requested_end)]
    if evaluation.empty:
        raise ValueError("backtest interval contains no trading sessions")
    applied = apply_resolved_strategy(replay_data.adjusted, snapshot.resolved_rule)
    first_location = sessions.get_loc(evaluation[0])
    first_signal_location = max(0, int(first_location) - 1)
    visible = sessions[first_signal_location : sessions.get_loc(evaluation[-1]) + 1]
    next_sessions = pd.Series(sessions[1:], index=sessions[:-1])
    rows = []
    for signal_date in visible:
        target = int(applied.target_position.loc[signal_date])
        valid_session = next_sessions.get(signal_date, pd.NaT)
        rows.append(
            {
                "decision_id": _decision_id(snapshot.identity.reference, signal_date, target),
                "signal_date": signal_date,
                "valid_session": valid_session,
                "target_position": target,
                "factor_score": float(applied.scores.loc[signal_date]),
                "regime": (
                    None
                    if applied.regimes is None
                    else str(applied.regimes.loc[signal_date])
                ),
            }
        )
    return SignalReplay(
        snapshot=snapshot,
        decisions=pd.DataFrame(rows),
        calculation_start=sessions[0],
        calculation_end=sessions[-1],
        evaluation_start=evaluation[0],
        evaluation_end=evaluation[-1],
    )
