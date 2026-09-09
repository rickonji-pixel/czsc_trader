"""Minimal, auditable state machines for early signal-mechanism research."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import pandas as pd


@dataclass(frozen=True)
class PrototypeTargets:
    """Target positions and the daily decision audit that produced them."""

    target_position: pd.Series
    decisions: pd.DataFrame


def transition_into(states: pd.Series, expected: str) -> pd.Series:
    """Identify a transition into one primary state on a normalized date index."""
    values = states.astype("string")
    active = values.eq(expected).fillna(False)
    return active & ~active.shift(1, fill_value=False)


def build_minimal_prototype(
    entry_states: pd.Series,
    entry_state: str,
    risk_states: Sequence[tuple[str, pd.Series, str]],
    *,
    max_holding_sessions: int,
    target_position: float = 1.0,
) -> PrototypeTargets:
    """Build one long-only prototype from a fresh entry transition and risk states."""
    if max_holding_sessions < 1:
        raise ValueError("max_holding_sessions must be positive")
    if not 0.0 < float(target_position) <= 1.0:
        raise ValueError("target_position must be in (0, 1]")
    index = pd.DatetimeIndex(pd.to_datetime(entry_states.index).normalize(), name="dt")
    if index.has_duplicates or not index.is_monotonic_increasing:
        raise ValueError("signal index must be unique and increasing")
    entry = pd.Series(entry_states.to_numpy(), index=index, dtype="string")
    entry_events = transition_into(entry, entry_state)

    risks: list[tuple[str, pd.Series]] = []
    for label, states, expected in risk_states:
        aligned = pd.Series(states.to_numpy(), index=pd.to_datetime(states.index), dtype="string")
        aligned.index = aligned.index.normalize()
        aligned = aligned.reindex(index)
        risks.append((label, aligned.eq(expected).fillna(False)))

    in_position = False
    entry_index: int | None = None
    rows: list[dict[str, object]] = []
    for offset, session in enumerate(index):
        active_risks = [label for label, active in risks if bool(active.iloc[offset])]
        entry_event = bool(entry_events.iloc[offset])
        action = "HOLD_CASH"
        held_sessions = 0 if entry_index is None else max(0, offset - entry_index)

        if in_position:
            action = "HOLD_POSITION"
            if active_risks:
                in_position = False
                entry_index = None
                action = "EXIT_RISK"
            elif held_sessions >= max_holding_sessions:
                in_position = False
                entry_index = None
                action = "EXIT_TIME"
            elif entry_event:
                action = "IGNORE_ENTRY_WHILE_HOLDING"
        elif entry_event and active_risks:
            action = "BLOCK_ENTRY_RISK"
        elif entry_event:
            in_position = True
            entry_index = offset
            held_sessions = 0
            action = "ENTER"

        rows.append({
            "date": session,
            "entry_state": None if pd.isna(entry.iloc[offset]) else str(entry.iloc[offset]),
            "entry_transition": entry_event,
            "risk_active": bool(active_risks),
            "active_risks": "|".join(active_risks),
            "held_sessions": held_sessions,
            "action": action,
            "target_position": float(target_position) if in_position else 0.0,
        })

    decisions = pd.DataFrame(rows)
    targets = decisions.set_index("date")["target_position"].rename("target_position")
    targets.index.name = "dt"
    return PrototypeTargets(target_position=targets, decisions=decisions)
