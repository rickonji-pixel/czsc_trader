from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from czsc_trader.causal_feature_gate_runtime import (
    score_feature_panel,
    support_manifest_path,
    support_panel_path,
)
from czsc_trader.experiment_archive import resolve_repository_experiment_reference
from czsc_trader.identity import raw_file_sha256

from .datasets import ReplayData
from .models import StrategySnapshot
from .signal_replay import SignalReplay


def _decision_id(reference: str, signal_date: pd.Timestamp, target: int) -> str:
    raw = f"{reference}|{signal_date.date()}|{target}".encode()
    return "DEC-" + sha256(raw).hexdigest()[:20].upper()


def _read_panel(path: Path) -> pd.DataFrame:
    panel = pd.read_csv(path, compression="gzip")
    if "date" not in panel:
        raise ValueError("causal-feature-gate panel has no date column")
    panel["date"] = pd.to_datetime(panel["date"], errors="raise").dt.normalize()
    if panel["date"].duplicated().any():
        raise ValueError("causal-feature-gate panel contains duplicate sessions")
    return panel.set_index("date").sort_index()


def _resolve_panel(
    snapshot: StrategySnapshot,
    replay_data: ReplayData,
    repository_root: Path,
    requested_end: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, str]]:
    spec = snapshot.resolved_rule.causal_feature_gate
    assert spec is not None
    frozen_source = resolve_repository_experiment_reference(repository_root, spec.source_path)
    frozen = _read_panel(frozen_source)
    if requested_end <= frozen.index[-1]:
        return frozen, {
            "mode": "frozen_research_evidence",
            "path": spec.source_path,
            "sha256": spec.source_sha256,
            "last_session": frozen.index[-1].date().isoformat(),
        }
    if replay_data.dataset != "backtest":
        raise ValueError(
            "requested interval exceeds frozen research evidence; "
            "use the backtest dataset with independently published strategy support data"
        )
    panel_path = support_panel_path(replay_data.root, snapshot.identity.reference)
    manifest_path = support_manifest_path(replay_data.root, snapshot.identity.reference)
    if not panel_path.is_file() or not manifest_path.is_file():
        raise ValueError(
            "backtest strategy support data is missing; run data prepare-strategy-support "
            f"for {snapshot.identity.reference} through the requested end"
        )
    identity = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual_hash = raw_file_sha256(panel_path)
    expected = {
        "release_id": snapshot.identity.reference,
        "source_path": spec.source_path,
        "source_sha256": spec.source_sha256,
        "panel_sha256": actual_hash,
        "features": sorted(dict(spec.orientations)),
    }
    if any(identity.get(key) != value for key, value in expected.items()):
        raise ValueError("backtest strategy support identity differs")
    panel = _read_panel(panel_path)
    if requested_end > panel.index[-1]:
        raise ValueError(
            "backtest strategy support data does not cover the requested end: "
            f"{panel.index[-1].date().isoformat()} < {requested_end.date().isoformat()}"
        )
    try:
        relative_path = panel_path.resolve().relative_to(repository_root.resolve()).as_posix()
    except ValueError:
        relative_path = str(panel_path.resolve())
    return panel, {
        "mode": "backtest_strategy_support",
        "path": relative_path,
        "sha256": actual_hash,
        "last_session": panel.index[-1].date().isoformat(),
    }


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
    replay_sessions = pd.DatetimeIndex(
        pd.to_datetime(replay_data.adjusted.daily["dt"], errors="raise").dt.normalize(),
        name="dt",
    )
    evaluation = replay_sessions[
        (replay_sessions >= requested_start) & (replay_sessions <= requested_end)
    ]
    if evaluation.empty:
        raise ValueError("backtest interval contains no trading sessions")
    panel, support_data = _resolve_panel(
        snapshot, replay_data, repository_root, evaluation[-1]
    )
    orientations = dict(spec.orientations)
    missing = sorted(set(orientations).difference(panel.columns))
    if missing:
        raise ValueError(f"causal-feature-gate panel is missing features: {missing}")
    if not evaluation.isin(panel.index).all():
        raise ValueError("causal-feature-gate panel does not cover the requested interval")
    sessions = replay_sessions[
        (replay_sessions >= panel.index.min()) & (replay_sessions <= evaluation[-1])
    ]
    if not sessions.isin(panel.index).all():
        raise ValueError("causal-feature-gate panel does not cover the replay calendar")
    panel = panel.reindex(sessions)
    base_score, confirmation_score, target = score_feature_panel(panel, spec)
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
        support_data=support_data,
    )
