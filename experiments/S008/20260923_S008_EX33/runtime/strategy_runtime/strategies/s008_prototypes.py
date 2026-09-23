"""Research-only executable implementations of the three S008 prototypes."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import replace
from datetime import date
from typing import Any, Mapping

import numpy as np
import pandas as pd
from dataflows import Dataset
from strategy_runtime import StrategyCandidate, StrategyImplementation, TradableWindow
from strategy_runtime.calculation import (
    CalculationScope,
    CalendarWindow,
    InputRange,
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


_MARKET = "adjusted_daily"
_SHIBOR = "shibor_daily"
_FX = "usdcnh_daily"
_SSE = "sse_daily"
_SSE_BASIC = "sse_daily_basic"
_CPI = "cpi_monthly"
_EXECUTION = "execution_daily"
_CALENDAR = "trading_calendar"

_ORIENTATIONS = {
    "price_return_120d": -1,
    "price_return_5d": -1,
    "price_trend_distance_20": -1,
    "currency_usdcnh_return_20d": 1,
    "rate_shibor_overnight_level": -1,
    "risk_sse_drawdown_60": -1,
    "risk_sse_return_20d": -1,
    "risk_sse_return_5d": -1,
    "risk_sse_turnover_z20": -1,
    "macro_domestic_real_cash_proxy": -1,
    "price_volatility_ratio_20_60": 1,
    "tsfresh_log_volume_change_absolute_sum_of_changes_60": -1,
    "tsfresh_price_intraday_range_mean_abs_change_60": -1,
}

_GROUPS = {
    "opportunity": ("currency_usdcnh_return_20d",),
    "entry_timing": (
        "price_return_5d",
        "price_return_120d",
        "price_trend_distance_20",
    ),
    "liquidity_context": (
        "rate_shibor_overnight_level",
        "macro_domestic_real_cash_proxy",
    ),
    "equity_stress_context": (
        "risk_sse_drawdown_60",
        "risk_sse_return_5d",
        "risk_sse_return_20d",
        "risk_sse_turnover_z20",
    ),
    "market_state_context": (
        "price_volatility_ratio_20_60",
        "tsfresh_log_volume_change_absolute_sum_of_changes_60",
        "tsfresh_price_intraday_range_mean_abs_change_60",
    ),
}

_PROTOTYPES = {
    "S008-P01-COMPOSITE-GATE",
    "S008-P02-REGIME-RECOVERY",
    "S008-P03-SAFE-HAVEN-PULSE",
}


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeContractError(f"{field_name} must be a mapping")
    return value


def _daily(frame: pd.DataFrame, field_name: str) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or "Date" not in frame:
        raise RuntimeContractError(f"{field_name} must contain Date")
    value = frame.copy()
    value["Date"] = pd.to_datetime(value["Date"], errors="raise").dt.normalize()
    if value["Date"].duplicated().any():
        raise RuntimeContractError(f"{field_name} contains duplicate dates")
    return value.set_index("Date").sort_index()


def _numeric(frame: pd.DataFrame, column: str, field_name: str) -> pd.Series:
    if column not in frame:
        raise RuntimeContractError(f"{field_name} misses {column}")
    return pd.to_numeric(frame[column], errors="raise").astype(float)


def _zscore(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window).mean()
    std = series.rolling(window).std(ddof=0).replace(0, np.nan)
    return (series - mean) / std


def _monthly_causal_cpi(frame: pd.DataFrame, sessions: pd.DatetimeIndex) -> pd.Series:
    values = frame.reset_index().sort_values("Date")
    if "NationalYoYPercent" not in values:
        raise RuntimeContractError("cpi_monthly misses NationalYoYPercent")
    available_calendar = values["Date"] + pd.offsets.MonthBegin(2)
    positions = sessions.searchsorted(available_calendar)
    inside = positions < len(sessions)
    effective = values.loc[inside, ["NationalYoYPercent"]].copy()
    effective.index = sessions[positions[inside]]
    duplicate_sessions = effective.index[effective.index.duplicated(keep=False)].unique()
    if any(item != sessions[0] for item in duplicate_sessions):
        raise RuntimeContractError("causal CPI effective sessions are not unique")
    effective = effective.loc[~effective.index.duplicated(keep="last")]
    return pd.to_numeric(
        effective["NationalYoYPercent"], errors="raise"
    ).reindex(sessions).ffill()


def _causal_percentile(series: pd.Series, window: int, minimum: int) -> pd.Series:
    def rank_last(items: np.ndarray) -> float:
        current = items[-1]
        valid = items[np.isfinite(items)]
        if not np.isfinite(current) or len(valid) < minimum:
            return np.nan
        return float(
            (np.count_nonzero(valid < current) + 0.5 * np.count_nonzero(valid == current))
            / len(valid)
        )

    return series.astype(float).rolling(window, min_periods=minimum).apply(rank_last, raw=True)


def _mean_abs_change(items: np.ndarray) -> float:
    differences = np.abs(np.diff(items))
    return float(differences.mean()) if len(differences) else 0.0


def _absolute_sum_of_changes(items: np.ndarray) -> float:
    return float(np.abs(np.diff(items)).sum())


def materialize_components(inputs: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    market = _daily(inputs[_MARKET], _MARKET)
    sessions = pd.DatetimeIndex(market.index, name="Date")
    close = _numeric(market, "Close", _MARKET)
    high = _numeric(market, "High", _MARKET)
    low = _numeric(market, "Low", _MARKET)
    volume = _numeric(market, "Volume", _MARKET)

    shibor = _daily(inputs[_SHIBOR], _SHIBOR)
    overnight = _numeric(shibor, "OvernightRate", _SHIBOR).reindex(sessions).ffill(limit=4)
    fx = _daily(inputs[_FX], _FX)
    fx_mid = (
        (_numeric(fx, "BidClose", _FX) + _numeric(fx, "AskClose", _FX))
        / 2.0
    ).reindex(sessions).ffill(limit=7).shift(1)
    sse = _daily(inputs[_SSE], _SSE)
    sse_close = _numeric(sse, "Close", _SSE).reindex(sessions).ffill(limit=1)
    sse_basic = _daily(inputs[_SSE_BASIC], _SSE_BASIC)
    turnover = _numeric(
        sse_basic, "TurnoverRateFreeFloat", _SSE_BASIC
    ).reindex(sessions).ffill(limit=1)
    cpi = _monthly_causal_cpi(_daily(inputs[_CPI], _CPI), sessions)

    returns_1d = close.pct_change(fill_method=None)
    volatility_20 = returns_1d.rolling(20).std(ddof=0)
    volatility_60 = returns_1d.rolling(60).std(ddof=0)
    intraday_range = (high - low) / close
    log_volume_change = np.log(volume.replace(0, np.nan)).diff()

    panel = pd.DataFrame(index=sessions)
    panel["price_return_120d"] = close.pct_change(120, fill_method=None)
    panel["price_return_5d"] = close.pct_change(5, fill_method=None)
    panel["price_trend_distance_20"] = close / close.rolling(20).mean() - 1.0
    panel["currency_usdcnh_return_20d"] = fx_mid.pct_change(20, fill_method=None)
    panel["rate_shibor_overnight_level"] = overnight
    panel["risk_sse_drawdown_60"] = sse_close / sse_close.rolling(60).max() - 1.0
    panel["risk_sse_return_20d"] = sse_close.pct_change(20, fill_method=None)
    panel["risk_sse_return_5d"] = sse_close.pct_change(5, fill_method=None)
    panel["risk_sse_turnover_z20"] = _zscore(turnover, 20)
    panel["macro_domestic_real_cash_proxy"] = overnight - cpi
    panel["price_volatility_ratio_20_60"] = volatility_20 / volatility_60.replace(0, np.nan)
    panel["tsfresh_log_volume_change_absolute_sum_of_changes_60"] = (
        log_volume_change.rolling(60, min_periods=60).apply(
            _absolute_sum_of_changes, raw=True
        )
    )
    panel["tsfresh_price_intraday_range_mean_abs_change_60"] = (
        intraday_range.rolling(60, min_periods=60).apply(_mean_abs_change, raw=True)
    )
    if set(panel.columns) != set(_ORIENTATIONS):
        raise RuntimeContractError("materialized component identity differs from EX19")
    return panel.loc[:, list(_ORIENTATIONS)]


def normalize_components(
    panel: pd.DataFrame, lookback_sessions: int, minimum_observations: int
) -> pd.DataFrame:
    normalized = pd.DataFrame(index=panel.index)
    for feature, orientation in _ORIENTATIONS.items():
        percentile = _causal_percentile(
            panel[feature], lookback_sessions, minimum_observations
        )
        normalized[feature] = percentile if orientation > 0 else 1.0 - percentile
    return normalized


def _weighted_score(
    normalized: pd.DataFrame,
    features: tuple[str, ...],
    weights: Mapping[str, float] | None = None,
) -> pd.Series:
    if weights is None:
        vector = pd.Series(1.0, index=features)
    else:
        vector = pd.Series({name: float(weights[name]) for name in features})
    if not np.isfinite(vector).all() or (vector < 0).any() or vector.sum() <= 0:
        raise RuntimeContractError("prototype weights must be finite, non-negative and non-zero")
    vector /= vector.sum()
    return normalized.loc[:, list(features)].mul(vector, axis=1).sum(
        axis=1, min_count=len(features)
    )


def _context_score(normalized: pd.DataFrame, rule: Mapping[str, Any]) -> pd.Series:
    context_weights = _mapping(rule["context_weights"], "context_weights")
    group_scores = pd.DataFrame(
        {
            group: _weighted_score(normalized, _GROUPS[group])
            for group in (
                "liquidity_context",
                "equity_stress_context",
                "market_state_context",
            )
        }
    )
    return _weighted_score(
        group_scores,
        tuple(group_scores.columns),
        context_weights,
    )


def _common_scores(
    normalized: pd.DataFrame, rule: Mapping[str, Any]
) -> pd.DataFrame:
    timing_weights = rule.get("timing_weights")
    if timing_weights is not None:
        timing_weights = _mapping(timing_weights, "timing_weights")
    return pd.DataFrame(
        {
            "opportunity_score": normalized["currency_usdcnh_return_20d"],
            "context_score": _context_score(normalized, rule),
            "timing_score": _weighted_score(
                normalized, _GROUPS["entry_timing"], timing_weights
            ),
            "state_stability_score": _weighted_score(
                normalized, _GROUPS["market_state_context"]
            ),
        },
        index=normalized.index,
    )


def _composite_gate(scores: pd.DataFrame, rule: Mapping[str, Any]) -> pd.DataFrame:
    current = 0
    targets: list[int] = []
    actions: list[str] = []
    for row in scores.itertuples():
        action = "HOLD_POSITION" if current else "HOLD_CASH"
        valid = np.isfinite(
            [row.opportunity_score, row.context_score, row.timing_score]
        ).all()
        if valid and current == 0 and (
            row.opportunity_score >= float(rule["opportunity_entry"])
            and row.context_score >= float(rule["context_entry"])
            and row.timing_score >= float(rule["timing_entry"])
        ):
            current = 1
            action = "ENTER"
        elif valid and current == 1 and (
            row.opportunity_score < float(rule["opportunity_exit"])
            or row.context_score < float(rule["context_exit"])
        ):
            current = 0
            action = "EXIT"
        targets.append(current)
        actions.append(action)
    return scores.assign(target_position=targets, action=actions)


def _regime_recovery(scores: pd.DataFrame, rule: Mapping[str, Any]) -> pd.DataFrame:
    state = "FLAT"
    armed_age = 0
    holding_age = 0
    armed_peak = np.nan
    targets: list[int] = []
    states: list[str] = []
    actions: list[str] = []
    for row in scores.itertuples():
        action = "HOLD_POSITION" if state == "LONG" else "HOLD_CASH"
        regime_values = np.asarray(
            [row.opportunity_score, row.context_score], dtype=float
        )
        finite_regime_values = regime_values[np.isfinite(regime_values)]
        regime = (
            float(finite_regime_values.mean())
            if len(finite_regime_values)
            else np.nan
        )
        if not np.isfinite(regime) or not np.isfinite(row.timing_score):
            targets.append(1 if state == "LONG" else 0)
            states.append(state)
            actions.append(action)
            continue
        if state == "FLAT" and (
            regime >= float(rule["regime_entry"])
            and row.timing_score >= float(rule["oversold_arm"])
        ):
            state = "ARMED"
            armed_age = 0
            armed_peak = row.timing_score
            action = "ARM"
        elif state == "ARMED":
            armed_age += 1
            armed_peak = max(float(armed_peak), row.timing_score)
            if regime < float(rule["regime_exit"]) or armed_age > int(
                rule["maximum_wait_sessions"]
            ):
                state = "FLAT"
                action = "DISARM"
            elif armed_peak - row.timing_score >= float(rule["recovery_delta"]):
                state = "LONG"
                holding_age = 0
                action = "ENTER"
        elif state == "LONG":
            holding_age += 1
            if regime < float(rule["regime_exit"]) or holding_age >= int(
                rule["maximum_hold_sessions"]
            ):
                state = "FLAT"
                action = "EXIT"
        targets.append(1 if state == "LONG" else 0)
        states.append(state)
        actions.append(action)
    return scores.assign(target_position=targets, state=states, action=actions)


def _safe_haven_pulse(
    normalized: pd.DataFrame, scores: pd.DataFrame, rule: Mapping[str, Any]
) -> pd.DataFrame:
    shock_features = (
        "currency_usdcnh_return_20d",
        "risk_sse_drawdown_60",
        "risk_sse_return_5d",
        "risk_sse_return_20d",
        "risk_sse_turnover_z20",
    )
    entry_count = normalized.loc[:, list(shock_features)].ge(
        float(rule["shock_quantile"])
    ).sum(axis=1)
    exit_count = normalized.loc[:, list(shock_features)].ge(
        float(rule["shock_exit_quantile"])
    ).sum(axis=1)
    current = 0
    holding_age = 0
    targets: list[int] = []
    actions: list[str] = []
    for index, row in enumerate(scores.itertuples()):
        action = "HOLD_POSITION" if current else "HOLD_CASH"
        valid = np.isfinite([row.timing_score, row.state_stability_score]).all()
        if valid and current == 0 and (
            entry_count.iloc[index] >= int(rule["shock_quorum"])
            and row.timing_score >= float(rule["timing_minimum"])
            and row.state_stability_score >= float(rule["state_stability_minimum"])
        ):
            current = 1
            holding_age = 0
            action = "ENTER"
        elif current == 1:
            holding_age += 1
            if exit_count.iloc[index] < int(rule["shock_quorum"]) or holding_age >= int(
                rule["maximum_hold_sessions"]
            ):
                current = 0
                action = "EXIT"
        targets.append(current)
        actions.append(action)
    return scores.assign(
        shock_entry_count=entry_count,
        shock_exit_count=exit_count,
        target_position=targets,
        action=actions,
    )


def calculate_prototype_history(
    inputs: Mapping[str, pd.DataFrame], parameters: Mapping[str, Any]
) -> pd.DataFrame:
    normalization = _mapping(parameters["normalization"], "normalization")
    rule = _mapping(parameters["rule"], "rule")
    panel = materialize_components(inputs)
    normalized = normalize_components(
        panel,
        int(normalization["lookback_sessions"]),
        int(normalization["minimum_observations"]),
    )
    scores = _common_scores(normalized, rule)
    prototype_id = str(parameters["prototype_id"])
    if prototype_id == "S008-P01-COMPOSITE-GATE":
        result = _composite_gate(scores, rule)
    elif prototype_id == "S008-P02-REGIME-RECOVERY":
        result = _regime_recovery(scores, rule)
    elif prototype_id == "S008-P03-SAFE-HAVEN-PULSE":
        result = _safe_haven_pulse(normalized, scores, rule)
    else:
        raise RuntimeContractError(f"unsupported S008 prototype: {prototype_id}")
    result["prototype_id"] = prototype_id
    return result


class S008Prototype(StrategyImplementation):
    """One parameterized implementation shared by all three frozen prototypes."""

    def __init__(self, candidate: StrategyCandidate) -> None:
        payload = candidate.payload
        parameters = _mapping(payload["parameters"], "parameters")
        prototype_id = str(parameters.get("prototype_id", ""))
        if prototype_id not in _PROTOTYPES:
            raise RuntimeContractError("S008 prototype identity is invalid")
        runtime = _mapping(payload["runtime"], "runtime")
        self._parameters = parameters
        requirements = (
            InputRequirement(
                _MARKET, Dataset.ETF_OHLCV.value, "518880.SH", "daily", 400,
                CutoffRule.SIGNAL_SESSION,
            ),
            InputRequirement(
                _SHIBOR, Dataset.SHIBOR_DAILY.value, None, "daily", 400,
                CutoffRule.SIGNAL_SESSION,
            ),
            InputRequirement(
                _FX, Dataset.USDCNH_DAILY.value, None, "daily", 400,
                CutoffRule.LATEST_AVAILABLE, 7,
            ),
            InputRequirement(
                _SSE, Dataset.DOMESTIC_INDEX_DAILY.value, "000001.SH", "daily", 400,
                CutoffRule.SIGNAL_SESSION,
            ),
            InputRequirement(
                _SSE_BASIC, Dataset.INDEX_DAILY_BASIC.value, "000001.SH", "daily", 400,
                CutoffRule.SIGNAL_SESSION,
            ),
            InputRequirement(
                _CPI, Dataset.CN_CPI_MONTHLY.value, None, "monthly", 1,
                CutoffRule.LATEST_AVAILABLE, 62,
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
                {"prototype_id": prototype_id, "component_count": 13},
            ),
            capabilities=RequiredCapabilities(
                tuple(sorted({item.dataset for item in requirements})),
                ("MARKET",),
            ),
            state_mode="STATELESS",
            identity_kind="CANDIDATE",
            candidate_id=candidate.candidate_id,
            history=HistoryPolicy("CANONICAL_REPLAY", "2013-07-29", "2012-01-02"),
        )

    @classmethod
    def from_candidate(cls, candidate: StrategyCandidate) -> "S008Prototype":
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
        scope = next_session_calculation_scope(
            self._definition, tradable_window, calendar_dates
        )
        cpi = scope.inputs[_CPI]
        aligned_cpi = InputRange(
            cpi.start.replace(day=1),
            cpi.end.replace(day=monthrange(cpi.end.year, cpi.end.month)[1]),
            cpi.required_cutoff,
        )
        return replace(scope, inputs={**scope.inputs, _CPI: aligned_cpi})

    def calculate_history(
        self,
        inputs: Mapping[str, pd.DataFrame],
        sessions: pd.DatetimeIndex,
    ) -> pd.DataFrame:
        history = calculate_prototype_history(inputs, self._parameters)
        requested = pd.DatetimeIndex(sessions).normalize()
        missing = requested.difference(history.index)
        if not missing.empty:
            raise RuntimeContractError(
                "S008 prototype history misses calculation sessions: "
                + ",".join(item.date().isoformat() for item in missing)
            )
        return history.reindex(requested)
