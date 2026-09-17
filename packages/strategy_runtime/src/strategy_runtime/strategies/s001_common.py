"""Shared frozen runtime implementation for the S001 strategy family."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta
import re
from types import MappingProxyType
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import czsc
import numpy as np
import pandas as pd
from dataflows import DataRequest, DataResult, DataStatus, Dataflows, Dataset

from ..errors import RuntimeContractError
from ..implementation_identity import implementation_sha256
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
_INTRADAY = "adjusted_30m"
_DAILY = "adjusted_daily"
_WEEKLY = "adjusted_weekly"
_EXECUTION = "execution_daily"
_CALENDAR = "trading_calendar"

_INTRADAY_CONFIG = (
    {"name": "cxt_bi_status_V230101", "freq": "30分钟"},
    {"name": "cxt_third_buy_V230228", "freq": "30分钟", "di": 1},
)
_DAILY_CONFIG = (
    {"name": "cxt_bi_status_V230101", "freq": "日线"},
    {"name": "cxt_five_bi_V230619", "freq": "日线", "di": 1},
    {"name": "cxt_seven_bi_V230620", "freq": "日线", "di": 1},
    *(
        {
            "name": "tas_ma_base_V221101",
            "freq": "日线",
            "di": 1,
            "timeperiod": period,
            "ma_type": "SMA",
        }
        for period in (5, 10, 20)
    ),
    {
        "name": "tas_macd_base_V221028",
        "freq": "日线",
        "di": 1,
        "fastperiod": 12,
        "slowperiod": 26,
        "signalperiod": 9,
    },
    {"name": "vol_window_V230731", "freq": "日线", "di": 1, "w": 5, "m": 30, "n": 10},
    {"name": "pressure_support_V240406", "freq": "日线", "di": 1, "w": 20},
)
_WEEKLY_CONFIG = ({"name": "cxt_bi_status_V230101", "freq": "周线"},)
_FALLBACK_WEIGHTS = {
    "raw__30m__cxt_bi_status_V230101": 0.05947026720107206,
    "raw__30m__cxt_third_buy_V230228__di_1": 0.05961894286907473,
    "raw__daily__cxt_bi_status_V230101": 0.04757621376085765,
    "raw__daily__cxt_five_bi_V230619__di_1": 0.05976947698292745,
    "raw__daily__cxt_seven_bi_V230620__di_1": 0.04757621376085765,
    "raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_5": 0.08370999965363214,
    "raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_10": 0.07136432064128646,
    "raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_20": 0.07136432064128646,
    "raw__daily__tas_macd_base_V221028__di_1__fastperiod_12__signalperiod_9__slowperiod_26": 0.07136432064128646,
    "raw__daily__vol_window_V230731__di_1__m_30__n_10__w_5": 0.19030485504343062,
    "raw__daily__pressure_support_V240406__di_1__w_20": 0.19030485504343062,
    "raw__weekly__cxt_bi_status_V230101": 0.04757621376085765,
}


def _source_sha256(strategy_class: type) -> str:
    wrapper = strategy_class.__module__.rsplit(".", 1)[-1]
    return implementation_sha256(
        (
            f"strategies/{wrapper}.py",
            "strategies/s001_common.py",
            "execution_planner.py",
        )
    )


def _object(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeContractError(f"{field_name} must be an object")
    return value


def _status(results: Mapping[str, DataResult]) -> PublicationStatus:
    statuses = {item.status for item in results.values()}
    if statuses == {DataStatus.READY}:
        return PublicationStatus.READY
    if DataStatus.FAILED in statuses:
        return PublicationStatus.FAILED
    if DataStatus.WAITING_SOURCE in statuses:
        return PublicationStatus.WAITING_SOURCE
    return PublicationStatus.INCOMPLETE


def _ohlcv(dataframe: pd.DataFrame, symbol: str) -> pd.DataFrame:
    frame = dataframe.rename(
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
    return frame[["dt", "symbol", "open", "high", "low", "close", "vol", "amount"]]


def _config_label(freq_tag: str, config: Mapping[str, object]) -> str:
    parameters = [f"{key}_{config[key]}" for key in sorted(config) if key not in {"name", "freq"}]
    suffix = "__" + "__".join(parameters) if parameters else ""
    return f"raw__{freq_tag}__{config['name']}{suffix}"


def _signals(
    frame: pd.DataFrame,
    freq: str,
    configs: tuple[Mapping[str, object], ...],
    init_n: int,
    freq_tag: str,
) -> pd.DataFrame:
    bars = czsc.format_standard_kline(
        frame[["dt", "symbol", "open", "close", "high", "low", "vol", "amount"]],
        freq=freq,
    )
    pieces = []
    for config in configs:
        output = czsc.generate_czsc_signals(
            bars,
            [dict(config)],
            sdt=str(frame["dt"].min().date()),
            init_n=init_n,
            df=True,
        )
        index = pd.DatetimeIndex(pd.to_datetime(output["dt"]), name="dt")
        if index.tz is not None:
            index = index.tz_localize(None)
        columns = [column for column in output.columns if len(column.split("_")) == 3]
        if len(columns) != 1:
            raise RuntimeContractError(f"CZSC {freq} returned unexpected signal columns")
        pieces.append(
            pd.Series(
                output[columns[0]].astype("string").to_numpy(),
                index=index,
                name=_config_label(freq_tag, config),
            )
        )
    return pd.concat(pieces, axis=1)


def _primary(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    return str(value).split("_", 1)[0]


def _signal_score(value: object, unknown: Counter[str]) -> float:
    primary = _primary(value)
    if primary is None:
        return 0.0
    if any(token in primary for token in ("向上", "多头", "三买", "底背驰", "支撑位", "强势")):
        return 1.0
    if any(token in primary for token in ("向下", "空头", "顶背驰", "压力位", "弱势")):
        return -1.0
    volume_rank = re.search(r"高量N(\d+)", primary)
    if volume_rank:
        rank = int(volume_rank.group(1))
        return 1.0 if rank >= 7 else -1.0 if rank <= 3 else 0.0
    if any(token in primary for token in ("其他", "任意", "中性", "零轴附近")):
        return 0.0
    unknown[primary] += 1
    return 0.0


def _backward_align(source: pd.DataFrame, target: pd.DatetimeIndex) -> pd.DataFrame:
    left = pd.DataFrame({"dt": target})
    right = source.reset_index().sort_values("dt")
    return pd.merge_asof(left.sort_values("dt"), right, on="dt", direction="backward").set_index(
        "dt"
    )


def _factor_frame(
    intraday: pd.DataFrame,
    daily: pd.DataFrame,
    weekly: pd.DataFrame,
) -> pd.DataFrame:
    target = pd.DatetimeIndex(daily["dt"], name="dt")
    intra = _signals(intraday, "30分钟", _INTRADAY_CONFIG, 500, "30m")
    intra = intra.loc[intra.index.strftime("%H:%M") == "15:00"]
    intra.index = intra.index.normalize()
    day = _signals(daily, "日线", _DAILY_CONFIG, 30, "daily")
    day.index = day.index.normalize()
    week = _signals(weekly, "周线", _WEEKLY_CONFIG, 10, "weekly")
    week.index = week.index.normalize()
    raw = pd.concat(
        [intra.reindex(target), day.reindex(target), _backward_align(week, target)], axis=1
    )
    raw.index = target
    return raw.map(lambda value: _signal_score(value, Counter())).astype(float)


def _score(factors: pd.DataFrame, weights: Mapping[str, float]) -> pd.Series:
    names = tuple(weights)
    if list(factors.loc[:, list(names)].columns) != list(names):
        raise RuntimeContractError("S001 factor identities differ from frozen weights")
    values = pd.Series(weights, dtype=float)
    if not np.isfinite(values).all() or abs(float(values.abs().sum()) - 1.0) > 1e-12:
        raise RuntimeContractError("S001 weights must be finite with L1 norm one")
    clean = factors.loc[:, list(names)].fillna(0.0).astype(float)
    groups = {
        "structure": tuple(name for name in names if "cxt_" in name),
        "trend": tuple(name for name in names if "tas_ma_" in name or "tas_macd_" in name),
        "volume_position": tuple(
            name for name in names if "vol_window_" in name or "pressure_support_" in name
        ),
    }
    subtotals = np.column_stack(
        [
            (
                clean.loc[:, list(groups[group])].mean(axis=1).to_numpy()
                * float(values.loc[list(groups[group])].sum())
                + clean.loc[:, list(groups[group])].to_numpy()
                @ (
                    values.loc[list(groups[group])].to_numpy()
                    - float(values.loc[list(groups[group])].mean())
                )
            )
            for group in ("structure", "trend", "volume_position")
        ]
    )
    return pd.Series(subtotals @ np.ones(3, dtype=float), index=clean.index, name="factor_score")


def _regimes(close: pd.Series, lookback: int, threshold: float) -> pd.Series:
    known = close.astype(float).shift(1)
    net = known.sub(known.shift(lookback)).abs()
    path = known.diff().abs().rolling(lookback, min_periods=lookback).sum()
    er = net.div(path.where(path.gt(0.0)))
    labels = pd.Series("warmup", index=close.index, dtype="string", name="regime")
    tied = pd.Series(np.isclose(er, threshold, rtol=0.0, atol=1e-15), index=close.index)
    trend = er.notna() & (er.ge(threshold) | tied)
    labels.loc[er.notna() & ~trend] = "range"
    labels.loc[trend] = "trend"
    return labels


def _positions(
    scores: pd.Series,
    *,
    enter: float,
    exit_: float,
    confirm_days: int,
    min_hold_days: int,
    exit_confirm_days: int,
) -> pd.Series:
    position = 0.0
    confirmations = 0
    exit_confirmations = 0
    holding_days = 0
    output = []
    for score in scores.astype(float):
        if position == 0.0:
            confirmations = confirmations + 1 if score >= enter else 0
            if confirmations >= confirm_days:
                position = 1.0
                holding_days = 1
                confirmations = 0
                exit_confirmations = 0
        else:
            eligible = holding_days >= min_hold_days
            exit_confirmations = exit_confirmations + 1 if eligible and score <= exit_ else 0
            if exit_confirmations >= exit_confirm_days:
                position = 0.0
                holding_days = 0
                confirmations = 0
                exit_confirmations = 0
            else:
                holding_days += 1
        output.append(position)
    return pd.Series(output, index=scores.index, name="target_position", dtype=float)


def calculate_s001_history(
    intraday: pd.DataFrame,
    daily: pd.DataFrame,
    weekly: pd.DataFrame,
    rule: Mapping[str, Any],
    symbol: str,
) -> pd.DataFrame:
    intra = _ohlcv(intraday, symbol)
    day = _ohlcv(daily, symbol)
    week = _ohlcv(weekly, symbol)
    factors = _factor_frame(intra, day, week)
    names = tuple(str(value) for value in rule["factor_names"])
    weights_by_regime = _object(rule["weights"], "S001 weights")
    regimes = _regimes(
        day.set_index("dt")["close"].reindex(factors.index),
        int(rule["er_lookback"]),
        float(rule["er_threshold"]),
    )
    scores = pd.Series(index=factors.index, dtype=float, name="factor_score")
    if names != tuple(_FALLBACK_WEIGHTS):
        raise RuntimeContractError("S001 factor identities differ from frozen fallback weights")
    fallback = _FALLBACK_WEIGHTS
    for label in ("trend", "range", "warmup"):
        selected = fallback if label == "warmup" else dict(weights_by_regime[label])
        mask = regimes.eq(label)
        if mask.any():
            scores.loc[mask] = _score(factors.loc[mask, list(names)], selected)
    targets = _positions(
        scores,
        enter=float(rule["entry_threshold"]),
        exit_=float(rule["exit_threshold"]),
        confirm_days=int(rule["confirm_days"]),
        min_hold_days=int(rule["min_hold_days"]),
        exit_confirm_days=int(rule["exit_confirm_days"]),
    )
    return pd.DataFrame({"factor_score": scores, "regime": regimes, "target_position": targets})


class S001Base:
    """Common executable behavior for immutable S001 releases."""

    expected_release_id = ""

    def __init__(self, release: StrategyRelease) -> None:
        if release.release_id != self.expected_release_id:
            raise RuntimeContractError(
                f"{self.__class__.__name__} can only load {self.expected_release_id}"
            )
        payload = _object(release.payload, "strategy payload")
        rule = _object(payload.get("rule"), "S001 rule")
        execution = _object(rule.get("execution"), "S001 execution")
        instrument = _object(execution.get("instrument"), "S001 instrument")
        symbol = str(instrument.get("symbol", "")).upper()
        if symbol != "588080.SH":
            raise RuntimeContractError("S001 frozen symbol must be 588080.SH")
        self._release = release
        self._rule = rule
        self._symbol = symbol
        order_types = tuple(
            sorted(
                {
                    str(_object(execution.get("entry"), "entry execution").get("order_type")),
                    str(_object(execution.get("exit"), "exit execution").get("order_type")),
                }
            )
        )
        datasets = (
            Dataset.ETF_OHLCV.value,
            Dataset.ETF_UNADJUSTED_DAILY.value,
            Dataset.TRADING_CALENDAR.value,
        )
        self._definition = RuntimeDefinition(
            1,
            release.strategy_family_id,
            release.version,
            release.release_id,
            release.release_hash,
            ImplementationRef(
                self.__class__.__module__,
                self.__class__.__name__,
                1,
                _source_sha256(self.__class__),
            ),
            ParameterSet(release.payload),
            InputContract(
                (
                    InputRequirement(
                        _INTRADAY,
                        Dataset.ETF_OHLCV.value,
                        symbol,
                        "30m",
                        500,
                        CutoffRule.SIGNAL_SESSION,
                    ),
                    InputRequirement(
                        _DAILY,
                        Dataset.ETF_OHLCV.value,
                        symbol,
                        "daily",
                        60,
                        CutoffRule.SIGNAL_SESSION,
                    ),
                    InputRequirement(
                        _WEEKLY,
                        Dataset.ETF_OHLCV.value,
                        symbol,
                        "weekly",
                        10,
                        CutoffRule.LATEST_AVAILABLE,
                        7,
                    ),
                    InputRequirement(
                        _EXECUTION,
                        Dataset.ETF_UNADJUSTED_DAILY.value,
                        symbol,
                        "daily",
                        1,
                        CutoffRule.SIGNAL_SESSION,
                    ),
                    InputRequirement(
                        _CALENDAR,
                        Dataset.TRADING_CALENDAR.value,
                        "SSE",
                        "daily",
                        0,
                        CutoffRule.LATEST_AVAILABLE,
                    ),
                )
            ),
            DecisionContract("TARGET_POSITION", 0.0, 1.0, "NEXT_SESSION_OPEN"),
            ExecutionPolicy("FROZEN_RULE", execution),
            MonitoringPolicy("FORWARD_OBSERVATION", {"frozen": True}),
            RequiredCapabilities(datasets, order_types),
        )

    @classmethod
    def from_release(cls, release: StrategyRelease) -> "S001Base":
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
        return calculate_s001_history(
            inputs[_INTRADAY],
            inputs[_DAILY],
            inputs[_WEEKLY],
            self._rule,
            self._symbol,
        )

    def publish_data(
        self, dataflows: Dataflows, deployment: DeploymentSpec, through: datetime
    ) -> PublishedStrategyData:
        cutoff = through.astimezone(_SHANGHAI).date()
        start = cutoff - timedelta(days=900)
        options = {}
        if deployment.settings.get("env_file") is not None:
            options["env_file"] = str(deployment.settings["env_file"])
        requests = {
            _INTRADAY: DataRequest(
                Dataset.ETF_OHLCV,
                self._symbol,
                start.isoformat(),
                cutoff.isoformat(),
                cutoff.isoformat(),
                "30m",
                options,
            ),
            _DAILY: DataRequest(
                Dataset.ETF_OHLCV,
                self._symbol,
                start.isoformat(),
                cutoff.isoformat(),
                cutoff.isoformat(),
                "daily",
                options,
            ),
            _WEEKLY: DataRequest(
                Dataset.ETF_OHLCV,
                self._symbol,
                start.isoformat(),
                cutoff.isoformat(),
                None,
                "weekly",
                options,
            ),
            _EXECUTION: DataRequest(
                Dataset.ETF_UNADJUSTED_DAILY,
                self._symbol,
                cutoff.isoformat(),
                cutoff.isoformat(),
                cutoff.isoformat(),
                "daily",
                options,
            ),
            _CALENDAR: DataRequest(
                Dataset.TRADING_CALENDAR,
                "SSE",
                cutoff.isoformat(),
                (cutoff + timedelta(days=20)).isoformat(),
                (cutoff + timedelta(days=20)).isoformat(),
                "daily",
                options,
            ),
        }
        results = {name: dataflows.fetch(request) for name, request in requests.items()}
        status = _status(results)
        error = None
        if status is not PublicationStatus.READY:
            error = "; ".join(
                f"{name}:{result.status.value}:{result.error.code if result.error else 'UNKNOWN'}"
                for name, result in results.items()
                if not result.ready
            )
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
        results = request.publication.input_results
        history = calculate_s001_history(
            results[_INTRADAY].dataframe,
            results[_DAILY].dataframe,
            results[_WEEKLY].dataframe,
            self._rule,
            self._symbol,
        )
        cutoff = pd.Timestamp(request.publication.requested_cutoff).normalize()
        if cutoff not in history.index:
            raise RuntimeContractError("S001 history does not reach requested cutoff")
        latest = history.loc[cutoff]
        execution = results[_EXECUTION].dataframe
        row = execution.loc[pd.to_datetime(execution["Date"]).dt.normalize().eq(cutoff)]
        if len(row) != 1:
            raise RuntimeContractError("S001 requires one execution-price row at cutoff")
        calendar = results[_CALENDAR].dataframe
        dates = pd.to_datetime(calendar["Date"]).dt.normalize()
        next_sessions = calendar.loc[
            dates.gt(cutoff) & calendar["IsOpen"].astype(int).eq(1), "Date"
        ]
        if next_sessions.empty:
            raise RuntimeContractError("S001 calendar has no next trading session")
        valid_at = datetime.combine(
            pd.Timestamp(next_sessions.iloc[0]).date(), time(9, 30), tzinfo=_SHANGHAI
        )
        identities = {name: result.identity.content_sha256 for name, result in results.items()}
        generated = request.calculation_time.astimezone(_SHANGHAI)
        target = float(latest["target_position"])
        suffix = canonical_sha256(
            {
                "release": self._release.release_hash,
                "cutoff": cutoff.date().isoformat(),
                "target": target,
                "inputs": identities,
            }
        )[:12].upper()
        previous = history["target_position"].shift(1, fill_value=0.0).loc[cutoff]
        action = "BUY" if target > previous else "SELL" if target < previous else "HOLD"
        return StrategyDecision(
            f"DEC-{generated:%Y%m%d-%H%M}-{suffix}",
            request.deployment.deployment_id,
            self._release.release_id,
            self._release.release_hash,
            self._definition.runtime_sha256,
            request.calculation_time,
            valid_at,
            target,
            request.account.revision,
            request.state.revision,
            identities,
            {
                "signal_date": cutoff.date().isoformat(),
                "action": action,
                "factor_score": float(latest["factor_score"]),
                "regime": str(latest["regime"]),
                "execution_reference_price": float(row.iloc[0]["Close"]),
            },
            {"signal_date": cutoff.date().isoformat(), "target_position": target, "action": action},
        )

    def explain(self, decision: StrategyDecision) -> StrategyExplanation:
        return StrategyExplanation(
            summary=f"{self._release.release_id} 综合结构、趋势和量价位置后给出 {decision.evidence['action']}",
            drivers=(
                f"综合得分：{float(decision.evidence['factor_score']):.4f}",
                f"市场状态：{decision.evidence['regime']}",
            ),
            risks=("多类技术信号在市场结构切换时可能同步失效",),
            details=MappingProxyType(dict(decision.evidence)),
        )
