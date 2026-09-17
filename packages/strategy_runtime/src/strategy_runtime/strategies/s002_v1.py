"""Frozen S002-v1 three-down-bars, five-session repair strategy."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import czsc
import pandas as pd
from dataflows import DataRequest, DataResult, DataStatus, Dataflows, Dataset

from ..errors import RuntimeContractError
from ..models import (
    CalculationRequest,
    CutoffRule,
    DecisionContract,
    DeploymentSpec,
    ExecutionPolicy,
    ImplementationRef,
    InputContract,
    InputRequirement,
    MonitoringPolicy,
    ParameterSet,
    PublicationStatus,
    PublishedStrategyData,
    RequiredCapabilities,
    RuntimeDefinition,
    StrategyDecision,
    StrategyExplanation,
    StrategyRelease,
    canonical_sha256,
)


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_INPUT_DAILY = "adjusted_daily"
_INPUT_EXECUTION = "execution_daily"
_INPUT_CALENDAR = "trading_calendar"


def _source_sha256() -> str:
    return sha256(Path(__file__).read_bytes()).hexdigest()


def _object(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeContractError(f"{field_name} must be an object")
    return value


def _publication_status(results: Mapping[str, DataResult]) -> PublicationStatus:
    statuses = {item.status for item in results.values()}
    if statuses == {DataStatus.READY}:
        return PublicationStatus.READY
    if DataStatus.FAILED in statuses:
        return PublicationStatus.FAILED
    if DataStatus.WAITING_SOURCE in statuses:
        return PublicationStatus.WAITING_SOURCE
    return PublicationStatus.INCOMPLETE


def _primary_value(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    return str(value).split("_", 1)[0]


def _target_history(states: pd.Series, entry_state: str, holding_sessions: int) -> pd.DataFrame:
    active = states.eq(entry_state).fillna(False)
    entries = active & ~active.shift(1, fill_value=False)
    in_position = False
    entry_offset: int | None = None
    rows: list[dict[str, object]] = []
    for offset, session in enumerate(states.index):
        entry = bool(entries.iloc[offset])
        held = 0 if entry_offset is None else max(0, offset - entry_offset)
        action = "HOLD_POSITION" if in_position else "HOLD_CASH"
        if in_position and held >= holding_sessions:
            in_position = False
            entry_offset = None
            action = "EXIT_TIME"
        elif in_position and entry:
            action = "IGNORE_ENTRY_WHILE_HOLDING"
        elif not in_position and entry:
            in_position = True
            entry_offset = offset
            action = "ENTER"
        rows.append(
            {
                "date": session,
                "signal_state": None if pd.isna(states.iloc[offset]) else str(states.iloc[offset]),
                "entry_transition": entry,
                "held_sessions": held,
                "action": action,
                "target_position": 1.0 if in_position else 0.0,
            }
        )
    return pd.DataFrame(rows).set_index("date")


def _calculate_target_history(
    daily: pd.DataFrame,
    *,
    symbol: str,
    signal: Mapping[str, Any],
    portfolio: Mapping[str, Any],
) -> pd.DataFrame:
    """Calculate the complete S002 target-position history from published daily bars."""
    frame = daily.rename(
        columns={
            "Date": "dt",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "vol",
            "Amount": "amount",
        }
    ).copy()
    frame["dt"] = pd.to_datetime(frame["dt"])
    frame.insert(1, "symbol", symbol)
    bars = czsc.format_standard_kline(
        frame[["dt", "symbol", "open", "close", "high", "low", "vol", "amount"]],
        freq="日线",
    )
    config = dict(_object(signal.get("config"), "S002-v1 signal config"))
    output = czsc.generate_czsc_signals(
        bars,
        [config],
        sdt=str(frame["dt"].min().date()),
        init_n=int(signal.get("warmup_bars", 250)),
        df=True,
    )
    output_key = str(signal.get("output_key", ""))
    if output_key not in output.columns:
        raise RuntimeContractError(f"S002-v1 signal output is missing: {output_key}")
    sessions = pd.DatetimeIndex(pd.to_datetime(output["dt"])).tz_localize(None).normalize()
    states = pd.Series(
        output[output_key].map(_primary_value).astype("string").to_numpy(),
        index=sessions,
    )
    history = _target_history(
        states,
        str(signal.get("entry_state", "")),
        int(portfolio.get("holding_sessions", 0)),
    )
    history["factor_score"] = (
        history["signal_state"].eq(str(signal.get("entry_state", ""))).astype(float)
    )
    return history


class S002V1:
    """Executable S002-v1 implementation with no TDR dependency."""

    def __init__(self, release: StrategyRelease) -> None:
        payload = _object(release.payload, "strategy payload")
        if payload.get("strategy_kind") != "czsc_event_hold":
            raise RuntimeContractError("S002-v1 strategy_kind must be czsc_event_hold")
        rule = _object(payload.get("rule"), "S002-v1 rule")
        signal = _object(rule.get("signal"), "S002-v1 signal")
        portfolio = _object(rule.get("portfolio_rule"), "S002-v1 portfolio rule")
        execution = _object(rule.get("execution"), "S002-v1 execution")
        instrument = _object(execution.get("instrument"), "S002-v1 instrument")
        symbol = str(rule.get("symbol", "")).upper()
        if symbol != "510500.SH" or str(instrument.get("symbol", "")).upper() != symbol:
            raise RuntimeContractError("S002-v1 frozen symbol must be 510500.SH")
        if signal.get("trigger") != "fresh_transition":
            raise RuntimeContractError("S002-v1 requires fresh_transition")
        if portfolio.get("ignore_entries_while_holding") is not True:
            raise RuntimeContractError("S002-v1 must ignore entries while holding")
        if portfolio.get("require_fresh_transition_after_exit") is not True:
            raise RuntimeContractError("S002-v1 must require a fresh transition after exit")
        self._release = release
        self._rule = rule
        self._signal = signal
        self._portfolio = portfolio
        self._symbol = symbol
        order_types = tuple(
            sorted(
                {
                    str(_object(execution.get("entry"), "entry execution").get("order_type")),
                    str(_object(execution.get("exit"), "exit execution").get("order_type")),
                }
            )
        )
        self._definition = RuntimeDefinition(
            schema_version=1,
            strategy_family_id=release.strategy_family_id,
            version=release.version,
            release_id=release.release_id,
            release_hash=release.release_hash,
            implementation=ImplementationRef(
                __name__, self.__class__.__name__, 1, _source_sha256()
            ),
            parameters=ParameterSet(release.payload),
            inputs=InputContract(
                (
                    InputRequirement(
                        _INPUT_DAILY,
                        Dataset.ETF_OHLCV.value,
                        symbol,
                        "daily",
                        300,
                        CutoffRule.SIGNAL_SESSION,
                    ),
                    InputRequirement(
                        _INPUT_EXECUTION,
                        Dataset.ETF_UNADJUSTED_DAILY.value,
                        symbol,
                        "daily",
                        1,
                        CutoffRule.SIGNAL_SESSION,
                    ),
                    InputRequirement(
                        _INPUT_CALENDAR,
                        Dataset.TRADING_CALENDAR.value,
                        "SSE",
                        "daily",
                        0,
                        CutoffRule.LATEST_AVAILABLE,
                    ),
                )
            ),
            decision=DecisionContract("TARGET_POSITION", 0.0, 1.0, "NEXT_SESSION_OPEN"),
            execution=ExecutionPolicy("FROZEN_RULE", execution),
            monitoring=MonitoringPolicy("FORWARD_OBSERVATION", {"frozen": True}),
            capabilities=RequiredCapabilities(
                (
                    Dataset.ETF_OHLCV.value,
                    Dataset.ETF_UNADJUSTED_DAILY.value,
                    Dataset.TRADING_CALENDAR.value,
                ),
                order_types,
            ),
        )

    @classmethod
    def from_release(cls, release: StrategyRelease) -> "S002V1":
        if release.release_id != "S002-v1":
            raise RuntimeContractError("S002V1 can only load S002-v1")
        return cls(release)

    @property
    def definition(self) -> RuntimeDefinition:
        return self._definition

    def calculate_history(
        self,
        inputs: Mapping[str, pd.DataFrame],
        sessions: pd.DatetimeIndex,
    ) -> pd.DataFrame:
        del sessions
        return _calculate_target_history(
            inputs[_INPUT_DAILY],
            symbol=self._symbol,
            signal=self._signal,
            portfolio=self._portfolio,
        )

    def publish_data(
        self,
        dataflows: Dataflows,
        deployment: DeploymentSpec,
        through: datetime,
    ) -> PublishedStrategyData:
        local = through.astimezone(_SHANGHAI)
        cutoff = local.date()
        start = cutoff - timedelta(days=650)
        calendar_end = cutoff + timedelta(days=20)
        options = {}
        env_file = deployment.settings.get("env_file")
        if env_file is not None:
            options["env_file"] = str(env_file)
        requests = {
            _INPUT_DAILY: DataRequest(
                Dataset.ETF_OHLCV,
                self._symbol,
                start.isoformat(),
                cutoff.isoformat(),
                cutoff.isoformat(),
                "daily",
                options,
            ),
            _INPUT_EXECUTION: DataRequest(
                Dataset.ETF_UNADJUSTED_DAILY,
                self._symbol,
                cutoff.isoformat(),
                cutoff.isoformat(),
                cutoff.isoformat(),
                "daily",
                options,
            ),
            _INPUT_CALENDAR: DataRequest(
                Dataset.TRADING_CALENDAR,
                "SSE",
                cutoff.isoformat(),
                calendar_end.isoformat(),
                calendar_end.isoformat(),
                "daily",
                options,
            ),
        }
        results = {name: dataflows.fetch(request) for name, request in requests.items()}
        status = _publication_status(results)
        error = None
        if status is not PublicationStatus.READY:
            failures = [
                f"{name}:{result.status.value}:{result.error.code if result.error else 'UNKNOWN'}"
                for name, result in results.items()
                if not result.ready
            ]
            error = "; ".join(failures)
        return PublishedStrategyData(
            self._release.release_id,
            self._release.release_hash,
            status,
            cutoff.isoformat(),
            requests,
            results,
            error,
        )

    def calculate(self, request: CalculationRequest) -> StrategyDecision:
        daily = request.publication.input_results[_INPUT_DAILY].dataframe.copy()
        history = _calculate_target_history(
            daily,
            symbol=self._symbol,
            signal=self._signal,
            portfolio=self._portfolio,
        )
        cutoff = pd.Timestamp(request.publication.requested_cutoff).normalize()
        if cutoff not in history.index:
            raise RuntimeContractError("S002-v1 signal history does not reach requested cutoff")
        latest = history.loc[cutoff]

        execution = request.publication.input_results[_INPUT_EXECUTION].dataframe
        execution_rows = execution.loc[pd.to_datetime(execution["Date"]).dt.normalize().eq(cutoff)]
        if len(execution_rows) != 1:
            raise RuntimeContractError("S002-v1 requires one execution-price row at cutoff")
        reference_price = float(execution_rows.iloc[0]["Close"])

        calendar = request.publication.input_results[_INPUT_CALENDAR].dataframe.copy()
        calendar_dates = pd.to_datetime(calendar["Date"]).dt.normalize()
        next_sessions = calendar.loc[
            calendar_dates.gt(cutoff) & calendar["IsOpen"].astype(int).eq(1), "Date"
        ]
        if next_sessions.empty:
            raise RuntimeContractError("S002-v1 calendar has no next trading session")
        valid_date = pd.Timestamp(next_sessions.iloc[0]).date()
        valid_at = datetime.combine(valid_date, time(9, 30), tzinfo=_SHANGHAI)
        generated = request.calculation_time.astimezone(_SHANGHAI)
        identity_payload = {
            name: result.identity.content_sha256
            for name, result in request.publication.input_results.items()
        }
        decision_suffix = canonical_sha256(
            {
                "release": self._release.release_hash,
                "cutoff": cutoff.date().isoformat(),
                "target": float(latest["target_position"]),
                "inputs": identity_payload,
            }
        )[:12].upper()
        return StrategyDecision(
            decision_id=f"DEC-{generated:%Y%m%d-%H%M}-{decision_suffix}",
            deployment_id=request.deployment.deployment_id,
            release_id=self._release.release_id,
            release_hash=self._release.release_hash,
            runtime_sha256=self._definition.runtime_sha256,
            generated_at=request.calculation_time,
            valid_at=valid_at,
            target_position=float(latest["target_position"]),
            account_revision=request.account.revision,
            state_revision=request.state.revision,
            input_identity_hashes=identity_payload,
            evidence={
                "signal_date": cutoff.date().isoformat(),
                "signal_state": latest["signal_state"],
                "entry_transition": bool(latest["entry_transition"]),
                "held_sessions": int(latest["held_sessions"]),
                "action": str(latest["action"]),
                "execution_reference_price": reference_price,
            },
            next_state={
                "signal_date": cutoff.date().isoformat(),
                "target_position": float(latest["target_position"]),
                "action": str(latest["action"]),
            },
        )

    def explain(self, decision: StrategyDecision) -> StrategyExplanation:
        action = str(decision.evidence.get("action", "UNKNOWN"))
        state = decision.evidence.get("signal_state")
        return StrategyExplanation(
            summary=f"S002-v1 根据三连跌新鲜切换与五日持有规则给出 {action}",
            drivers=(f"信号状态：{state}", f"目标仓位：{decision.target_position:.0%}"),
            risks=("短期均值修复可能在持续下跌行情中失效",),
            details=MappingProxyType(dict(decision.evidence)),
        )
