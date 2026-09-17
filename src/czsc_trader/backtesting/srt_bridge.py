"""TDR bridge from frozen SRT strategies to deterministic backtest channels."""

from __future__ import annotations

from datetime import datetime, time
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

import pandas as pd
from strategy_runtime import (
    AccountSnapshot,
    BacktestChannel,
    DeploymentSpec,
    ExecutionReceipt,
    StrategyDecision,
    StrategyLoader,
    StrategyRelease,
    StrategyRunner,
)

from .datasets import ReplayData
from .execution_replay import replay_account
from .intraday_overlay_replay import replay_intraday_overlay
from .models import StrategySnapshot
from .result import BacktestResult
from .signal_replay import SignalReplay


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _published(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rename(
        columns={
            "dt": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "vol": "Volume",
            "amount": "Amount",
        }
    ).drop(columns=["symbol"], errors="ignore")


def _decision_id(reference: str, signal_date: pd.Timestamp, target: int) -> str:
    raw = f"{reference}|{signal_date.date()}|{target}".encode()
    return "DEC-" + sha256(raw).hexdigest()[:20].upper()


def _overlay_decision_id(reference: str, signal_date: pd.Timestamp) -> str:
    raw = "|".join(map(str, (reference, signal_date, "INTRADAY_LONG"))).encode()
    return "DEC-" + sha256(raw).hexdigest()[:20].upper()


def _load_release(repository_root: Path, reference: str) -> StrategyRelease:
    family, version = reference.split("-", 1)
    path = Path(repository_root) / "strategies" / family / "versions" / f"{version}.json"
    return StrategyRelease.from_mapping(json.loads(path.read_text(encoding="utf-8")))


def _strategy_evidence(
    release: StrategyRelease,
    replay_data: ReplayData,
    repository_root: Path,
    requested_end: pd.Timestamp,
) -> pd.DataFrame | None:
    rule = release.payload.get("rule")
    source = rule.get("data_source") if isinstance(rule, Mapping) else None
    path_value = source.get("path") if isinstance(source, Mapping) else None
    if not isinstance(path_value, str):
        return None
    frozen_path = (Path(repository_root) / path_value).resolve()
    frozen = pd.read_csv(frozen_path)
    date_column = "date" if "date" in frozen else "dt"
    frozen_end = pd.to_datetime(frozen[date_column]).max().normalize()
    if requested_end <= frozen_end:
        return frozen
    safe = release.release_id.lower().replace("-", "_")
    matches = sorted(Path(replay_data.root).glob(f"{safe}_*_panel.csv.gz"))
    if len(matches) != 1:
        raise ValueError(f"SRT historical support data is unavailable for {release.release_id}")
    support = pd.read_csv(matches[0])
    support_end = pd.to_datetime(support[date_column]).max().normalize()
    if requested_end > support_end:
        raise ValueError(f"SRT historical support data ends at {support_end.date().isoformat()}")
    return support


def build_srt_signal_replay(
    *,
    snapshot: StrategySnapshot,
    replay_data: ReplayData,
    start: pd.Timestamp,
    end: pd.Timestamp,
    repository_root: Path,
) -> tuple[object, SignalReplay]:
    """Calculate one complete historical decision series inside its SRT class."""

    release = _load_release(repository_root, snapshot.identity.reference)
    strategy = StrategyLoader().load(release)
    sessions = pd.DatetimeIndex(
        pd.to_datetime(replay_data.adjusted.daily["dt"]).dt.normalize(), name="dt"
    )
    evaluation = sessions[(sessions >= start.normalize()) & (sessions <= end.normalize())]
    if evaluation.empty:
        raise ValueError("backtest interval contains no trading sessions")
    inputs = {
        "adjusted_30m": _published(replay_data.adjusted.intraday),
        "adjusted_daily": _published(replay_data.adjusted.daily),
        "adjusted_weekly": _published(replay_data.adjusted.weekly),
        "execution_daily": _published(replay_data.execution_daily),
    }
    evidence = _strategy_evidence(release, replay_data, repository_root, evaluation[-1])
    if evidence is not None:
        inputs["strategy_evidence"] = evidence
    history = strategy.calculate_history(inputs, sessions)
    first_location = int(sessions.get_loc(evaluation[0]))
    visible = sessions[max(0, first_location - 1) : int(sessions.get_loc(evaluation[-1])) + 1]
    next_sessions = pd.Series(sessions[1:], index=sessions[:-1])
    rows: list[dict[str, object]] = []
    output_kind = strategy.definition.decision.output_kind
    if output_kind == "INTRADAY_OVERLAY":
        selected = history.loc[history["signal_active"].fillna(False).astype(bool)].copy()
        selected["valid_session"] = selected.index.map(next_sessions)
        selected = selected.loc[selected["valid_session"].isin(evaluation)]
        for signal_date, row in selected.iterrows():
            rows.append(
                {
                    "decision_id": _overlay_decision_id(snapshot.identity.reference, signal_date),
                    "signal_date": signal_date,
                    "valid_session": row["valid_session"],
                    "target_position": 1,
                    "factor_score": float(row["moneyflow_breadth"]),
                    "regime": None,
                    "threshold": float(row["threshold"]),
                    "observed_weight_ratio": float(row["observed_weight_ratio"]),
                    "action": "INTRADAY_LONG_OVERLAY",
                }
            )
        chart_data = (
            history.reindex(evaluation)
            .reset_index(names="date")
            .rename(columns={"moneyflow_breadth": "factor_score"})
        )
    else:
        for signal_date in visible:
            row = history.loc[signal_date]
            target = int(row["target_position"])
            record: dict[str, object] = {
                "decision_id": _decision_id(snapshot.identity.reference, signal_date, target),
                "signal_date": signal_date,
                "valid_session": next_sessions.get(signal_date, pd.NaT),
                "target_position": target,
                "factor_score": float(row.get("factor_score", row.get("base_score", target))),
            }
            if "confirmation_score" in history:
                record["confirmation_score"] = float(row["confirmation_score"])
            record["regime"] = row.get("regime")
            rows.append(record)
        chart_data = pd.DataFrame(
            {
                "date": visible,
                "factor_score": [
                    float(
                        history.loc[item].get(
                            "factor_score", history.loc[item].get("base_score", 0.0)
                        )
                    )
                    for item in visible
                ],
                "target_position": [
                    float(history.loc[item, "target_position"]) for item in visible
                ],
            }
        )
        if "regime" in history:
            chart_data["regime"] = history.loc[visible, "regime"].astype("string").to_numpy()
    calculations = history.index
    replay = SignalReplay(
        snapshot=snapshot,
        decisions=pd.DataFrame(rows),
        calculation_start=pd.Timestamp(calculations.min()),
        calculation_end=pd.Timestamp(calculations.max()),
        evaluation_start=evaluation[0],
        evaluation_end=evaluation[-1],
        chart_data=chart_data,
    )
    return strategy, replay


class _BufferedExecutionModel:
    def __init__(self) -> None:
        self.decision_ids: list[str] = []

    def execute(self, request, idempotency_key: str) -> ExecutionReceipt:
        self.decision_ids.append(request.decision.decision_id)
        return ExecutionReceipt(
            idempotency_key,
            True,
            request.decision.decision_id,
            "BUFFERED",
            "accepted for deterministic batch replay",
        )


def replay_srt_account(
    *,
    strategy,
    signals: SignalReplay,
    replay_data: ReplayData,
    initial_cash: float,
) -> BacktestResult:
    """Route SRT decisions through StrategyRunner and BacktestChannel, then settle."""

    model = _BufferedExecutionModel()
    definition = strategy.definition
    channel = BacktestChannel(
        model,
        order_types=definition.capabilities.order_types,
        checkpoints=definition.capabilities.checkpoints,
    )
    deployment = DeploymentSpec(
        "backtest-deployment",
        definition.release_id,
        definition.release_hash,
        replay_data.adjusted.symbol,
        "backtest-account",
        channel.channel_id,
    )
    identity_hash = sha256(
        f"{replay_data.fingerprint}|{definition.runtime_sha256}".encode()
    ).hexdigest()
    runner = StrategyRunner()
    valid = signals.decisions.dropna(subset=["valid_session"])
    for row in valid.itertuples(index=False):
        signal_date = pd.Timestamp(row.signal_date)
        generated_at = datetime.combine(signal_date.date(), time(20, 30), _SHANGHAI)
        valid_session = pd.Timestamp(row.valid_session)
        valid_at = datetime.combine(valid_session.date(), time(9, 30), _SHANGHAI)
        account = AccountSnapshot(
            deployment.account_id,
            initial_cash,
            initial_cash,
            0,
            0,
            generated_at,
        )
        decision = StrategyDecision(
            str(row.decision_id),
            deployment.deployment_id,
            definition.release_id,
            definition.release_hash,
            definition.runtime_sha256,
            generated_at,
            valid_at,
            float(row.target_position),
            0,
            0,
            {"historical_replay": identity_hash},
            {"signal_date": signal_date.date().isoformat()},
            {"target_position": float(row.target_position)},
        )
        runner.submit_precomputed(
            strategy=strategy,
            deployment=deployment,
            account_snapshot=account,
            channel=channel,
            decision=decision,
        )
    if model.decision_ids != valid["decision_id"].astype(str).tolist():
        raise ValueError("backtest channel decision sequence differs from SRT replay")
    if definition.decision.output_kind == "INTRADAY_OVERLAY":
        return replay_intraday_overlay(signals, replay_data, initial_cash)
    return replay_account(signals, replay_data, initial_cash)
