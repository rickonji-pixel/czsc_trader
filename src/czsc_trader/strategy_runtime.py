"""Execute a resolved strategy through one advice/backtest runtime boundary."""

from __future__ import annotations

import czsc
import pandas as pd

from .baseline_execution import apply_resolved_baseline
from .baselines import EventHoldSpec, ResolvedBaseline
from .data import MarketData
from .factors import _to_raw_bars, generate_factor_frame
from .rules import AppliedRule
from .signal_census import primary_value
from .signal_prototypes import build_minimal_prototype


def _event_hold_states(data: MarketData, spec: EventHoldSpec) -> pd.Series:
    bars = _to_raw_bars(data.daily, spec.signal_frequency)
    output = czsc.generate_czsc_signals(
        bars,
        [dict(spec.signal_config)],
        sdt=str(pd.Timestamp(data.daily["dt"].min()).date()),
        init_n=spec.warmup_bars,
        df=True,
    )
    if spec.output_key not in output.columns:
        raise ValueError(
            f"event-hold signal output is missing: {spec.output_key}"
        )
    generated_index = pd.DatetimeIndex(
        pd.to_datetime(output["dt"]).dt.normalize(), name="dt"
    )
    if generated_index.tz is not None:
        generated_index = generated_index.tz_localize(None)
    if generated_index.has_duplicates:
        raise ValueError("event-hold signal output contains duplicate sessions")
    states = pd.Series(
        output[spec.output_key].map(primary_value).astype("string").to_numpy(),
        index=generated_index,
        name="signal_state",
    )
    target_index = pd.DatetimeIndex(
        pd.to_datetime(data.daily["dt"]).dt.normalize(), name="dt"
    )
    return states.reindex(target_index)


def _apply_event_hold(data: MarketData, spec: EventHoldSpec) -> AppliedRule:
    states = _event_hold_states(data, spec)
    prototype = build_minimal_prototype(
        states,
        spec.entry_state,
        [],
        max_holding_sessions=spec.holding_sessions,
        target_position=spec.target_position,
    )
    target = prototype.target_position.astype(float)
    scores = states.eq(spec.entry_state).fillna(False).astype(float)
    scores.name = "factor_score"
    audit = prototype.decisions.set_index("date")
    previous = target.shift(1, fill_value=0.0)
    rows: list[dict[str, object]] = []
    for signal_date in target.index[target.ne(previous)]:
        after = float(target.loc[signal_date])
        rows.append(
            {
                "event_id": (
                    f"EventHold:{pd.Timestamp(signal_date):%Y%m%d}:"
                    f"{'Entry' if after else 'Exit'}"
                ),
                "signal_date": pd.Timestamp(signal_date),
                "event_type": "Entry" if after else "Exit",
                "factor_score": float(scores.loc[signal_date]),
                "before_position": float(previous.loc[signal_date]),
                "after_position": after,
                "signal_state": (
                    None if pd.isna(states.loc[signal_date]) else str(states.loc[signal_date])
                ),
                "reason": str(audit.loc[signal_date, "action"]),
            }
        )
    return AppliedRule(target, scores, pd.DataFrame(rows))


def apply_resolved_strategy(data: MarketData, strategy: ResolvedBaseline) -> AppliedRule:
    """Return the common strategy application contract for every supported kind."""
    if strategy.strategy == "czsc_event_hold":
        if strategy.event_hold is None:
            raise ValueError("event-hold strategy has no event specification")
        if data.symbol.upper() != strategy.event_hold.symbol:
            raise ValueError("event-hold market data symbol differs from strategy")
        return _apply_event_hold(data, strategy.event_hold)

    factors = generate_factor_frame(data)
    sessions = pd.DatetimeIndex(pd.to_datetime(data.daily["dt"]), name="dt")
    close = pd.Series(
        data.daily["close"].astype(float).to_numpy(),
        index=sessions,
        name="close",
    )
    return apply_resolved_baseline(factors.frame, strategy, daily_close=close)
