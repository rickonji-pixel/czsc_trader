"""TDR bridge from candidate/frozen SRT strategies to TXE historical execution."""

from __future__ import annotations

from datetime import datetime, time
from hashlib import sha256
import json
from pathlib import Path
from collections.abc import Mapping
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from strategy_runtime import (
    DeploymentSpec,
    ExecutionPricingData,
    PublishedStrategyData,
    StrategyDecision,
    StrategyLoader,
    StrategyRelease,
    StrategyRunner,
    StrategyCandidate,
    RuntimeContractError,
    StrategyRuntimeContext,
    effective_target_order_type,
    load_strategy_runtime_context,
    read_publication,
    canonical_sha256,
)

from trading_execution_engine import HistoricalExecutor
from .datasets import ReplayData
from .models import StrategySnapshot
from .result import BacktestResult
from .signal_replay import SignalReplay


def _validate_historical_decisions(
    history: pd.DataFrame,
    required_sessions: pd.DatetimeIndex,
) -> None:
    """Reject incomplete historical outputs before absence becomes HOLD or NO_EVENT."""

    if not isinstance(history.index, pd.DatetimeIndex):
        raise RuntimeContractError("SRT historical decisions must use a DatetimeIndex")
    normalized = pd.DatetimeIndex(pd.to_datetime(history.index).normalize(), name="dt")
    if normalized.has_duplicates or not normalized.is_monotonic_increasing:
        raise RuntimeContractError("SRT historical decision sessions are invalid")
    normalized_history = history.copy()
    normalized_history.index = normalized
    missing = required_sessions.difference(normalized_history.index)
    if not missing.empty:
        raise RuntimeContractError(
            "SRT historical decisions do not cover required sessions: "
            f"missing={[item.date().isoformat() for item in missing]}"
        )
    visible = normalized_history.reindex(required_sessions)
    if "target_position" not in visible:
        raise RuntimeContractError("SRT historical decisions have no target_position")
    target = pd.to_numeric(visible["target_position"], errors="coerce")
    if target.isna().any() or not target.between(0.0, 1.0).all():
        raise RuntimeContractError(
            "SRT historical target_position contains unavailable or invalid values"
        )
    for column in visible.columns:
        if column == "target_position" or not pd.api.types.is_numeric_dtype(visible[column]):
            continue
        values = pd.to_numeric(visible[column], errors="coerce")
        if values.isna().any() or not np.isfinite(values.to_numpy(dtype=float)).all():
            raise RuntimeContractError(
                f"SRT historical decision field is unavailable: {column}"
            )


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _plain_json(value):
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


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


def load_srt_strategy(
    repository_root: Path,
    reference: str,
    *,
    deployment_symbol: str | None = None,
):
    """Load a frozen runtime, optionally bound to an explicit backtest symbol."""

    release = _load_release(repository_root, reference)
    loader = StrategyLoader()
    strategy = (
        loader.load_for_symbol(release, deployment_symbol)
        if deployment_symbol is not None
        else loader.load(release)
    )
    return release, strategy


def strategy_reference_symbol(strategy) -> str:
    """Return the single ETF instrument declared by one frozen runtime."""

    subjects = {
        item.subject.upper()
        for item in strategy.definition.inputs.requirements
        if item.subject and item.dataset.startswith("etf.")
    }
    if len(subjects) != 1:
        payload = strategy.definition.parameters.values
        rule = payload.get("rule")
        if isinstance(rule, Mapping):
            execution = rule.get("execution")
            if isinstance(execution, Mapping):
                instrument = execution.get("instrument")
                if isinstance(instrument, Mapping) and isinstance(
                    instrument.get("symbol"), str
                ):
                    return str(instrument["symbol"]).upper()
            if isinstance(rule.get("symbol"), str):
                return str(rule["symbol"]).upper()
        if isinstance(payload.get("symbol"), str):
            return str(payload["symbol"]).upper()
        raise ValueError("SRT strategy does not declare exactly one ETF instrument")
    return next(iter(subjects))


def execution_intraday_frequencies(strategy) -> tuple[str, ...]:
    """Map SRT channel checkpoints to TDR execution-price datasets."""

    checkpoints = set(strategy.definition.capabilities.checkpoints)
    unsupported = checkpoints - {"OPEN", "11:30_CLOSE"}
    if unsupported:
        raise ValueError(f"unsupported SRT execution checkpoints: {sorted(unsupported)}")
    return ("5m",) if "11:30_CLOSE" in checkpoints else ()


def build_srt_signal_replay(
    *,
    snapshot: StrategySnapshot,
    replay_data: ReplayData,
    start: pd.Timestamp,
    end: pd.Timestamp,
    repository_root: Path,
    publication: PublishedStrategyData | None = None,
) -> tuple[object, SignalReplay]:
    """Calculate one complete historical decision series inside its SRT class."""

    if snapshot.identity.kind == "CANDIDATE":
        family, separator, candidate_id = snapshot.identity.reference.partition("-")
        if not separator:
            raise RuntimeContractError("candidate replay requires a family-qualified identity")
        candidate = StrategyCandidate(family, candidate_id, snapshot.strategy_payload)
        strategy = StrategyLoader().load_candidate(candidate)
        if snapshot.source_hash != candidate.runtime_identity_sha256:
            raise RuntimeContractError("candidate release hashes differ from the snapshot")
        if snapshot.content_hash != canonical_sha256(snapshot.strategy_payload):
            raise RuntimeContractError("candidate snapshot content hash differs")
    elif snapshot.identity.kind == "REGISTERED":
        release, strategy = load_srt_strategy(
            repository_root, snapshot.identity.reference,
            deployment_symbol=replay_data.adjusted.symbol,
        )
        if (
            snapshot.source_hash != release.release_hash
            or canonical_sha256(snapshot.strategy_payload) != canonical_sha256(release.payload)
        ):
            raise RuntimeContractError("registered snapshot differs from its frozen release")
    else:
        raise RuntimeContractError(f"unsupported strategy identity: {snapshot.identity.kind}")
    if strategy.definition.release_id != snapshot.identity.reference:
        raise RuntimeContractError("historical runtime differs from the replay identity")
    if strategy_reference_symbol(strategy) != replay_data.adjusted.symbol:
        raise RuntimeContractError("historical runtime differs from the replay symbol")
    if replay_data.dataset == "research" and end.normalize() > pd.Timestamp(replay_data.cutoff):
        raise RuntimeContractError("research backtest window exceeds the published cutoff")
    sessions = pd.DatetimeIndex(
        pd.to_datetime(replay_data.adjusted.daily["dt"]).dt.normalize(), name="dt"
    )
    evaluation = sessions[(sessions >= start.normalize()) & (sessions <= end.normalize())]
    if evaluation.empty:
        raise ValueError("backtest interval contains no trading sessions")
    if publication is None and replay_data.dataset == "backtest":
        runtime_context = load_strategy_runtime_context(replay_data.root, strategy)
        publication = runtime_context.strategy_data
    else:
        if publication is None:
            publication = read_publication(
                replay_data.root, strategy.definition.release_id
            )
        runtime_context = StrategyRuntimeContext(
            publication,
            ExecutionPricingData(
                replay_data.adjusted.symbol,
                replay_data.adjusted.daily,
                replay_data.execution_daily,
            ),
        )
    StrategyRunner.validate_publication(strategy, publication)
    if not publication.ready:
        raise RuntimeContractError("historical calculation requires a READY publication")
    if replay_data.dataset == "research" and pd.Timestamp(publication.requested_cutoff).date() != replay_data.cutoff:
        raise RuntimeContractError("research publication cutoff differs from the controlled research dataset")
    if pd.Timestamp(publication.requested_cutoff) < evaluation[-1]:
        raise ValueError(
            "SRT historical publication ends before the requested backtest interval"
        )
    inputs = {
        name: result.dataframe
        for name, result in publication.input_results.items()
    }
    history = strategy.calculate_history(inputs, sessions)
    first_location = int(sessions.get_loc(evaluation[0]))
    visible = sessions[max(0, first_location - 1) : int(sessions.get_loc(evaluation[-1])) + 1]
    _validate_historical_decisions(history, visible)
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
            if float(row["target_position"]) not in (0.0, 1.0):
                raise RuntimeContractError("TXE target replay requires binary positions; fractional targets are unsupported")
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
        rows = [
            row
            for row in rows
            if not pd.isna(row["valid_session"])
            and pd.Timestamp(row["valid_session"]) in evaluation
        ]
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
        if "confirmation_score" in history:
            chart_data["confirmation_score"] = (
                history.loc[visible, "confirmation_score"].astype(float).to_numpy()
            )
        if "regime" in history:
            chart_data["regime"] = history.loc[visible, "regime"].astype("string").to_numpy()
    calculations = history.index
    execution = strategy.definition.execution
    target_order_types: dict[str, str | None] = {
        "entry_order_type": None,
        "exit_order_type": None,
    }
    if execution.policy_type == "FROZEN_RULE":
        target_order_types = {
            "entry_order_type": effective_target_order_type(execution.settings, "BUY"),
            "exit_order_type": effective_target_order_type(execution.settings, "SELL"),
        }
    replay = SignalReplay(
        snapshot=snapshot,
        decisions=pd.DataFrame(rows),
        calculation_start=pd.Timestamp(calculations.min()),
        calculation_end=pd.Timestamp(calculations.max()),
        evaluation_start=evaluation[0],
        evaluation_end=evaluation[-1],
        runtime_context=runtime_context,
        support_data={
            "mode": "srt_input_contract",
            "release_id": strategy.definition.release_id,
            "runtime_sha256": strategy.definition.runtime_sha256,
            "execution_policy": {
                "policy_type": execution.policy_type,
                "settings": _plain_json(execution.settings),
            },
            **target_order_types,
            "requested_cutoff": publication.requested_cutoff,
            "publication_sha256": sha256(
                json.dumps(
                    {
                        name: result.identity.content_sha256
                        for name, result in sorted(publication.input_results.items())
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
        },
        chart_data=chart_data,
    )
    return strategy, replay


def replay_srt_account(
    *,
    strategy,
    signals: SignalReplay,
    replay_data: ReplayData,
    initial_cash: float,
    fee_rate_override: float | None = None,
) -> BacktestResult:
    """Route SRT decisions directly through TXE, then wrap facts for reporting."""

    definition = strategy.definition
    channel = HistoricalExecutor(
        strategy_reference=signals.snapshot.identity.reference,
        execution_daily=replay_data.execution_daily,
        execution_intraday=replay_data.execution_intraday,
        execution_five_minute=replay_data.execution_five_minute,
        evaluation_start=signals.evaluation_start,
        evaluation_end=signals.evaluation_end,
        initial_cash=initial_cash,
        execution_policy=definition.execution,
        order_types=definition.capabilities.order_types,
        checkpoints=definition.capabilities.checkpoints,
        fee_rate_override=fee_rate_override,
    )
    publication_hash = (signals.support_data or {}).get("publication_sha256", "")
    identity_hash = sha256(
        f"{replay_data.fingerprint}|{publication_hash}|{definition.runtime_sha256}".encode()
    ).hexdigest()
    runner = StrategyRunner()
    valid = signals.decisions.dropna(subset=["valid_session"])
    for row in valid.itertuples(index=False):
        signal_date = pd.Timestamp(row.signal_date)
        generated_at = datetime.combine(signal_date.date(), time(20, 30), _SHANGHAI)
        valid_session = pd.Timestamp(row.valid_session)
        valid_at = datetime.combine(valid_session.date(), time(9, 30), _SHANGHAI)
        deployment = DeploymentSpec(
            "backtest-deployment",
            definition.release_id,
            definition.release_hash,
            replay_data.adjusted.symbol,
            "backtest-account",
            channel.channel_id,
            channel.deployment_settings,
        )
        account = channel.account_snapshot(deployment.account_id, generated_at)
        evidence: dict[str, object] = {"signal_date": signal_date.date().isoformat()}
        for key in (
            "factor_score",
            "confirmation_score",
            "regime",
            "threshold",
            "observed_weight_ratio",
            "action",
        ):
            if not hasattr(row, key):
                continue
            value = getattr(row, key)
            if pd.isna(value):
                continue
            evidence[key] = value.item() if hasattr(value, "item") else value
        decision = StrategyDecision(
            str(row.decision_id),
            deployment.deployment_id,
            definition.release_id,
            definition.release_hash,
            definition.runtime_sha256,
            generated_at,
            valid_at,
            float(row.target_position),
            account.revision,
            0,
            {"historical_replay": identity_hash},
            evidence,
            {"target_position": float(row.target_position)},
        )
        runner.submit_precomputed(
            strategy=strategy,
            deployment=deployment,
            account_snapshot=account,
            channel=channel,
            decision=decision,
            context=signals.runtime_context,
            execution_policy=channel.effective_policy,
        )
    ledger = channel.finalize()
    return BacktestResult(
        identity=signals.snapshot.identity,
        decisions=ledger.decisions,
        orders=ledger.orders,
        fills=ledger.fills,
        account_daily=ledger.account_daily,
        trades=ledger.trades,
    )
