"""Research-only implementation of the S008 precious-metal preference prototype."""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

import pandas as pd
from dataflows import Dataset
from strategy_runtime import StrategyCandidate, StrategyImplementation, TradableWindow
from strategy_runtime.calculation import (
    CalculationScope,
    CalendarWindow,
    next_session_calculation_scope,
    next_session_calendar_window,
)
from strategy_runtime.errors import RuntimeContractError
from strategy_runtime.models import (
    CutoffRule,
    DecisionContract,
    ExecutionPolicy,
    HistoryPolicy,
    ImplementationRef,
    InputContract,
    InputRequirement,
    MonitoringPolicy,
    ParameterSet,
    RequiredCapabilities,
    RuntimeDefinition,
)


_PROTOTYPE_ID = "S008-P04-PRECIOUS-METAL-PREFERENCE"
_MARKET = "adjusted_daily"
_XAU = "xauusd_daily"
_XAG = "xagusd_daily"
_EXECUTION = "execution_daily"
_CALENDAR = "trading_calendar"


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeContractError(f"{field_name} must be a mapping")
    return value


def _indexed(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    if "Date" not in frame.columns:
        raise RuntimeContractError(f"{name} has no Date column")
    result = frame.copy()
    result.index = pd.DatetimeIndex(
        pd.to_datetime(result.pop("Date"), errors="raise")
    ).tz_localize(None).normalize()
    if result.index.has_duplicates or not result.index.is_monotonic_increasing:
        raise RuntimeContractError(f"{name} dates must be unique and ordered")
    return result


def _mid_close(frame: pd.DataFrame, name: str) -> pd.Series:
    bid = pd.to_numeric(frame["BidClose"], errors="raise")
    ask = pd.to_numeric(frame["AskClose"], errors="raise")
    if (bid <= 0).any() or (ask <= 0).any() or (bid > ask).any():
        raise RuntimeContractError(f"{name} contains invalid close quotes")
    return (bid + ask) / 2.0


def calculate_preference_history(
    inputs: Mapping[str, pd.DataFrame],
    parameters: Mapping[str, Any],
) -> pd.DataFrame:
    """Materialize causal features and replay the frozen two-state machine."""

    market = _indexed(inputs[_MARKET], _MARKET)
    xau = _indexed(inputs[_XAU], _XAU)
    xag = _indexed(inputs[_XAG], _XAG)
    index = market.index

    # A China-session decision may only use a strictly earlier FXCM source date.
    xau_mid = _mid_close(xau, _XAU).reindex(index).ffill().shift(1)
    xag_mid = _mid_close(xag, _XAG).reindex(index).ffill().shift(1)
    xau_source_date = pd.Series(xau.index, index=xau.index).reindex(index).ffill().shift(1)
    xag_source_date = pd.Series(xag.index, index=xag.index).reindex(index).ffill().shift(1)

    ratio = xau_mid / xag_mid
    slow = ratio / ratio.rolling(120, min_periods=120).mean() - 1.0
    fast = xau_mid.pct_change(20, fill_method=None) - xag_mid.pct_change(
        20, fill_method=None
    )

    slow_entry = float(parameters["slow_entry"])
    slow_exit = float(parameters["slow_exit"])
    fast_entry = float(parameters["fast_entry"])
    fast_exit = float(parameters["fast_exit"])
    confirmation_sessions = int(parameters["confirmation_sessions"])
    minimum_hold_sessions = int(parameters["minimum_hold_sessions"])
    cooldown_sessions = int(parameters["cooldown_sessions"])
    if not slow_exit < slow_entry or not fast_exit < fast_entry:
        raise RuntimeContractError("entry and exit thresholds violate hysteresis")
    if min(confirmation_sessions, minimum_hold_sessions) < 1 or cooldown_sessions < 0:
        raise RuntimeContractError("state-machine durations are invalid")

    state = "FLAT"
    entry_streak = 0
    hold_sessions = 0
    cooldown_remaining = 0
    targets: list[float] = []
    states: list[str] = []
    actions: list[str] = []
    streaks: list[int] = []
    holds: list[int] = []
    cooldowns: list[int] = []
    for slow_value, fast_value in zip(slow, fast, strict=True):
        ready = pd.notna(slow_value) and pd.notna(fast_value)
        action = "HOLD_FLAT" if state == "FLAT" else "HOLD_LONG"
        if state == "FLAT":
            hold_sessions = 0
            if not ready:
                entry_streak = 0
                action = "WARMUP"
            elif cooldown_remaining > 0:
                cooldown_remaining -= 1
                entry_streak = 0
                action = "COOLDOWN"
            else:
                entry_streak = (
                    entry_streak + 1
                    if slow_value >= slow_entry and fast_value >= fast_entry
                    else 0
                )
                if entry_streak >= confirmation_sessions:
                    state = "LONG"
                    hold_sessions = 1
                    entry_streak = 0
                    action = "ENTER"
        else:
            hold_sessions += 1
            if (
                ready
                and hold_sessions >= minimum_hold_sessions
                and (slow_value <= slow_exit or fast_value <= fast_exit)
            ):
                state = "FLAT"
                hold_sessions = 0
                cooldown_remaining = cooldown_sessions
                entry_streak = 0
                action = "EXIT"
        targets.append(1.0 if state == "LONG" else 0.0)
        states.append(state)
        actions.append(action)
        streaks.append(entry_streak)
        holds.append(hold_sessions)
        cooldowns.append(cooldown_remaining)

    result = pd.DataFrame(
        {
            "gold_silver_ratio_distance_120": slow,
            "gold_minus_silver_return_20": fast,
            "xau_source_date": xau_source_date,
            "xag_source_date": xag_source_date,
            "target_position": targets,
            "state": states,
            "action": actions,
            "entry_streak": streaks,
            "hold_sessions": holds,
            "cooldown_remaining": cooldowns,
            "prototype_id": _PROTOTYPE_ID,
        },
        index=index,
    )
    ready = result["gold_silver_ratio_distance_120"].notna() & result[
        "gold_minus_silver_return_20"
    ].notna()
    if not (
        pd.to_datetime(result.loc[ready, "xau_source_date"]) < result.index[ready]
    ).all() or not (
        pd.to_datetime(result.loc[ready, "xag_source_date"]) < result.index[ready]
    ).all():
        raise RuntimeContractError("FXCM source date is not strictly prior to decision session")
    return result


class S008PreciousMetalPreference(StrategyImplementation):
    """Candidate-compatible research runtime for the frozen P04 prototype."""

    def __init__(self, candidate: StrategyCandidate) -> None:
        payload = candidate.payload
        parameters = _mapping(payload["parameters"], "parameters")
        if parameters.get("prototype_id") != _PROTOTYPE_ID:
            raise RuntimeContractError("S008 prototype identity is invalid")
        runtime = _mapping(payload["runtime"], "runtime")
        self._parameters = parameters
        requirements = (
            InputRequirement(
                _MARKET, Dataset.ETF_OHLCV.value, "518880.SH", "daily", 150,
                CutoffRule.SIGNAL_SESSION,
            ),
            InputRequirement(
                _XAU, Dataset.FXCM_DAILY.value, "XAUUSD.FXCM", "daily", 150,
                CutoffRule.PREVIOUS_SESSION,
            ),
            InputRequirement(
                _XAG, Dataset.FXCM_DAILY.value, "XAGUSD.FXCM", "daily", 150,
                CutoffRule.PREVIOUS_SESSION,
            ),
            InputRequirement(
                _EXECUTION, Dataset.ETF_UNADJUSTED_DAILY.value, "518880.SH", "daily", 1,
                CutoffRule.SIGNAL_SESSION,
            ),
            InputRequirement(
                _CALENDAR, Dataset.TRADING_CALENDAR.value, "SSE", "daily", 0,
                CutoffRule.LATEST_AVAILABLE,
            ),
        )
        execution = {
            "capital": {
                "fee_rate": 0.001,
                "mode": "full_available_cash",
                "target_scope": "entry_cycle",
            },
            "entry": {"order_type": "MARKET", "limit_parameter": 0.0},
            "exit": {"order_type": "MARKET", "limit_ratio": 0.1},
            "instrument": {
                "symbol": "518880.SH",
                "lot_size": 100,
                "maximum_order_quantity": 100000000,
                "price_limit_ratio": 0.1,
                "price_tick": 0.001,
            },
        }
        self._definition = RuntimeDefinition(
            schema_version=2,
            strategy_family_id=candidate.strategy_family_id,
            version=None,
            release_id=candidate.reference_id,
            release_hash=candidate.runtime_identity_sha256,
            implementation=ImplementationRef(
                str(runtime["module"]),
                str(runtime["qualname"]),
                int(runtime["contract_version"]),
                str(runtime["source_sha256"]),
            ),
            parameters=ParameterSet(parameters),
            inputs=InputContract(requirements),
            decision=DecisionContract("TARGET_POSITION", 0.0, 1.0, "NEXT_SESSION_OPEN"),
            execution=ExecutionPolicy("FROZEN_RULE", execution),
            monitoring=MonitoringPolicy(
                "FORWARD_OBSERVATION",
                {"prototype_id": _PROTOTYPE_ID, "component_count": 2},
            ),
            capabilities=RequiredCapabilities(
                tuple(sorted({item.dataset for item in requirements})),
                ("MARKET",),
            ),
            state_mode="STATELESS",
            identity_kind="CANDIDATE",
            candidate_id=candidate.candidate_id,
            history=HistoryPolicy("CANONICAL_REPLAY", "2018-01-02", "2017-01-02"),
        )

    @classmethod
    def from_candidate(
        cls, candidate: StrategyCandidate
    ) -> "S008PreciousMetalPreference":
        return cls(candidate)

    @property
    def definition(self) -> RuntimeDefinition:
        return self._definition

    def calendar_window(self, tradable_window: TradableWindow) -> CalendarWindow:
        return next_session_calendar_window(self._definition, tradable_window)

    def derive_calculation_scope(
        self,
        tradable_window: TradableWindow,
        calendar_dates: tuple[date, ...],
    ) -> CalculationScope:
        return next_session_calculation_scope(
            self._definition, tradable_window, calendar_dates
        )

    def calculate_history(
        self,
        inputs: Mapping[str, pd.DataFrame],
        sessions: pd.DatetimeIndex,
    ) -> pd.DataFrame:
        history = calculate_preference_history(inputs, self._parameters)
        requested = pd.DatetimeIndex(sessions).tz_localize(None).normalize()
        missing = requested.difference(history.index)
        if not missing.empty:
            raise RuntimeContractError(
                "S008 preference history misses calculation sessions: "
                + ",".join(item.date().isoformat() for item in missing)
            )
        return history.reindex(requested)
