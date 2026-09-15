from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pandas as pd

from czsc_trader.causal_feature_gate_runtime import score_feature_panel
from czsc_trader.experiment_archive import resolve_repository_experiment_reference

from .datasets import ReplayData
from .models import StrategySnapshot
from .signal_replay import SignalReplay


def _decision_id(reference: str, signal_date: pd.Timestamp, target: int) -> str:
    raw = f"{reference}|{signal_date.date()}|{target}".encode()
    return "DEC-" + sha256(raw).hexdigest()[:20].upper()


def build_causal_feature_gate_signals(
    snapshot: StrategySnapshot,
    replay_data: ReplayData,
    start: pd.Timestamp,
    end: pd.Timestamp,
    repository_root: Path,
) -> SignalReplay:
    """Rebuild a frozen weighted-score gate from its immutable causal feature panel."""
    spec = snapshot.resolved_rule.causal_feature_gate
    if spec is None:
        raise ValueError("strategy snapshot has no causal-feature-gate specification")
    requested_start = pd.Timestamp(start).normalize()
    requested_end = pd.Timestamp(end).normalize()
    if requested_start > requested_end:
        raise ValueError("backtest start must not be after end")
    source = resolve_repository_experiment_reference(repository_root, spec.source_path)
    panel = pd.read_csv(source, compression="gzip")
    if "date" not in panel:
        raise ValueError("causal-feature-gate panel has no date column")
    panel["date"] = pd.to_datetime(panel["date"], errors="raise").dt.normalize()
    if panel["date"].duplicated().any():
        raise ValueError("causal-feature-gate panel contains duplicate sessions")
    panel = panel.set_index("date").sort_index()
    orientations = dict(spec.orientations)
    missing = sorted(set(orientations).difference(panel.columns))
    if missing:
        raise ValueError(f"causal-feature-gate panel is missing features: {missing}")
    replay_sessions = pd.DatetimeIndex(
        pd.to_datetime(replay_data.adjusted.daily["dt"], errors="raise").dt.normalize(),
        name="dt",
    )
    sessions = replay_sessions[
        (replay_sessions >= panel.index.min()) & (replay_sessions <= panel.index.max())
    ]
    if not sessions.isin(panel.index).all():
        raise ValueError("causal-feature-gate panel does not cover the replay calendar")
    panel = panel.reindex(sessions)
    base_score, confirmation_score, target = score_feature_panel(panel, spec)
    evaluation = sessions[(sessions >= requested_start) & (sessions <= requested_end)]
    if evaluation.empty:
        raise ValueError("backtest interval contains no trading sessions")
    first_location = int(sessions.get_loc(evaluation[0]))
    if first_location == 0:
        raise ValueError("causal-feature-gate evaluation requires one prior signal session")
    visible = sessions[first_location - 1 : int(sessions.get_loc(evaluation[-1])) + 1]
    next_sessions = pd.Series(sessions[1:], index=sessions[:-1])
    rows: list[dict[str, object]] = []
    for signal_date in visible:
        desired = int(target.loc[signal_date])
        rows.append(
            {
                "decision_id": _decision_id(snapshot.identity.reference, signal_date, desired),
                "signal_date": signal_date,
                "valid_session": next_sessions.get(signal_date, pd.NaT),
                "target_position": desired,
                "factor_score": float(base_score.loc[signal_date]),
                "confirmation_score": float(confirmation_score.loc[signal_date]),
                "regime": None,
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
