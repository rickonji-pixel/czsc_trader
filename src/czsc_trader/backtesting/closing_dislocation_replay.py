from __future__ import annotations

from hashlib import sha256

import pandas as pd

from .datasets import ReplayData
from .models import StrategySnapshot
from .signal_replay import SignalReplay


def _decision_id(reference: str, signal_date: pd.Timestamp, target: int) -> str:
    raw = f"{reference}|{signal_date.date()}|{target}".encode()
    return "DEC-" + sha256(raw).hexdigest()[:20].upper()


def _path_efficiency(frame: pd.DataFrame) -> float:
    returns = frame["close"].pct_change(fill_method=None)
    first = float(frame.iloc[0]["close"] / frame.iloc[0]["open"] - 1.0)
    variation = float(returns.iloc[1:].abs().sum() + abs(first))
    displacement = abs(float(frame.iloc[-1]["close"] / frame.iloc[0]["open"] - 1.0))
    return displacement / variation if variation > 0.0 else 0.0


def _daily_features(one_minute: pd.DataFrame, late_window: int) -> pd.DataFrame:
    bars = one_minute.copy()
    bars["dt"] = pd.to_datetime(bars["dt"], errors="raise")
    bars["trade_date"] = bars["dt"].dt.normalize()
    rows: list[dict[str, object]] = []
    for trade_date, day in bars.groupby("trade_date", sort=True, observed=True):
        day = day.sort_values("dt")
        if len(day) != 240:
            raise ValueError(f"{trade_date.date()}: incomplete one-minute session")
        late = day.iloc[-late_window:]
        late_return = float(late.iloc[-1]["close"] / late.iloc[0]["open"] - 1.0)
        total_volume = float(day["vol"].sum())
        total_amount = float(day["amount"].sum())
        if total_volume <= 0 or total_amount <= 0:
            raise ValueError(f"{trade_date.date()}: non-positive session turnover")
        session_vwap = total_amount / total_volume
        close = float(day.iloc[-1]["close"])
        selloff = max(-late_return, 0.0)
        rows.append(
            {
                "trade_date": trade_date,
                "LATE_PATH_SELLING": selloff * _path_efficiency(late),
                "LATE_VOLUME_PRESSURE": selloff * float(late["vol"].sum()) / total_volume,
                "CLOSE_VWAP_DISLOCATION": max(session_vwap / close - 1.0, 0.0),
            }
        )
    return pd.DataFrame(rows).set_index("trade_date")


def _cooldown(raw_dates: pd.DatetimeIndex, calendar: pd.DatetimeIndex, sessions: int) -> set[pd.Timestamp]:
    positions = {value: index for index, value in enumerate(calendar)}
    kept: set[pd.Timestamp] = set()
    last = -10**9
    for value in raw_dates.sort_values():
        position = positions[value]
        if position - last > sessions:
            kept.add(value)
            last = position
    return kept


def build_closing_dislocation_signals(
    snapshot: StrategySnapshot,
    replay_data: ReplayData,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> SignalReplay:
    """Generate causal S004 daily targets from complete one-minute sessions."""
    spec = snapshot.resolved_rule.closing_dislocation_overnight
    if spec is None:
        raise ValueError("strategy snapshot has no closing-dislocation specification")
    if replay_data.signal_one_minute is None:
        raise ValueError("closing-dislocation replay requires one-minute signal data")
    requested_start = pd.Timestamp(start).normalize()
    requested_end = pd.Timestamp(end).normalize()
    if requested_start > requested_end:
        raise ValueError("backtest start must not be after end")
    features = _daily_features(replay_data.signal_one_minute, spec.late_window_minutes)
    sessions = pd.DatetimeIndex(pd.to_datetime(replay_data.adjusted.daily["dt"]), name="dt")
    if not features.index.equals(sessions):
        raise ValueError("one-minute feature sessions differ from daily signal sessions")
    evaluation = sessions[(sessions >= requested_start) & (sessions <= requested_end)]
    if evaluation.empty:
        raise ValueError("backtest interval contains no trading sessions")

    votes = pd.DataFrame(index=features.index)
    thresholds = pd.DataFrame(index=features.index)
    for mechanism in spec.mechanisms:
        score = features[mechanism]
        threshold = score.shift(spec.threshold_lag_sessions).rolling(
            spec.threshold_lookback_sessions,
            min_periods=spec.threshold_lookback_sessions,
        ).quantile(spec.threshold_quantile)
        thresholds[mechanism] = threshold
        votes[mechanism] = threshold.notna() & score.gt(0.0) & score.ge(threshold)
    vote_count = votes.sum(axis=1).astype(int)
    raw_dates = pd.DatetimeIndex(vote_count.index[vote_count.ge(spec.votes_required)])
    events = _cooldown(raw_dates, sessions, spec.cooldown_sessions)

    first_location = int(sessions.get_loc(evaluation[0]))
    first_signal_location = max(0, first_location - 1)
    visible = sessions[first_signal_location : int(sessions.get_loc(evaluation[-1])) + 1]
    next_sessions = pd.Series(sessions[1:], index=sessions[:-1])
    rows: list[dict[str, object]] = []
    for signal_date in visible:
        target = int(signal_date in events)
        row: dict[str, object] = {
            "decision_id": _decision_id(snapshot.identity.reference, signal_date, target),
            "signal_date": signal_date,
            "valid_session": next_sessions.get(signal_date, pd.NaT),
            "target_position": target,
            "factor_score": int(vote_count.loc[signal_date]),
            "regime": None,
        }
        for mechanism in spec.mechanisms:
            row[f"{mechanism}_score"] = float(features.loc[signal_date, mechanism])
            threshold = thresholds.loc[signal_date, mechanism]
            row[f"{mechanism}_threshold"] = (
                None if pd.isna(threshold) else float(threshold)
            )
            row[f"{mechanism}_vote"] = bool(votes.loc[signal_date, mechanism])
        rows.append(row)
    return SignalReplay(
        snapshot=snapshot,
        decisions=pd.DataFrame(rows),
        calculation_start=sessions[0],
        calculation_end=sessions[-1],
        evaluation_start=evaluation[0],
        evaluation_end=evaluation[-1],
    )
