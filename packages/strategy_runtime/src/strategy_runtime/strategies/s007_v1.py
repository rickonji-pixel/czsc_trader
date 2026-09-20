"""Frozen S007-v1 multi-source causal feature-gate runtime."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from types import MappingProxyType
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from dataflows import DataRequest, DataResult, DataStatus, Dataflows, Dataset

from ..errors import RuntimeContractError
from ..execution_rules import effective_target_order_type
from ..implementation_identity import implementation_sha256
from ..models import (
    CalculationRequest,
    CutoffRule,
    DecisionContract,
    DeploymentSpec,
    ExecutionPolicy,
    HistoryPolicy,
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
_MARKET = "adjusted_daily"
_SHIBOR = "shibor_daily"
_CHINEXT = "chinext_daily_basic"
_SHARES = "etf_share_size"
_SPX = "spx_daily"
_EXECUTION = "execution_daily"
_CALENDAR = "trading_calendar"
_EVIDENCE = "strategy_evidence"
_FROZEN_HISTORY_START = pd.Timestamp("2021-01-04")
_GLOBAL_HISTORY_START = pd.Timestamp("2020-12-01")


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


def causal_percentile(values: pd.Series, window: int, minimum: int) -> pd.Series:
    def rank_last(items: np.ndarray) -> float:
        current = items[-1]
        valid = items[np.isfinite(items)]
        if not np.isfinite(current) or len(valid) < minimum:
            return np.nan
        return float(
            (np.count_nonzero(valid < current) + 0.5 * np.count_nonzero(valid == current))
            / len(valid)
            - 0.5
        )

    return values.astype(float).rolling(window, min_periods=minimum).apply(rank_last, raw=True)


def materialize_s007_features(inputs: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    market = inputs[_MARKET].copy()
    market["Date"] = pd.to_datetime(market["Date"]).dt.normalize()
    market = market.drop_duplicates("Date", keep="last").set_index("Date").sort_index()
    sessions = market.index
    close = pd.to_numeric(market["Close"], errors="raise")
    volume = pd.to_numeric(market["Volume"], errors="raise")
    amount = pd.to_numeric(market["Amount"], errors="raise")
    output = pd.DataFrame(index=sessions)
    output["price_close_vwap_deviation"] = close / (amount / volume) - 1.0
    output["price_intraday_range"] = (
        pd.to_numeric(market["High"], errors="raise") - pd.to_numeric(market["Low"], errors="raise")
    ) / close
    output["tsfresh__log_volume_change__mean__lb20"] = (
        np.log(volume).diff().rolling(20, min_periods=20).mean()
    )

    shibor = inputs[_SHIBOR].drop_duplicates("Date", keep="last").copy()
    shibor["Date"] = pd.to_datetime(shibor["Date"]).dt.normalize()
    overnight = (
        shibor.set_index("Date")["OvernightRate"]
        .astype(float)
        .sort_index()
        .reindex(sessions)
        .ffill(limit=4)
    )
    output["risk_shibor_on_change_5d"] = overnight.diff(5)

    chinext = inputs[_CHINEXT].drop_duplicates("Date", keep="last").copy()
    chinext["Date"] = pd.to_datetime(chinext["Date"]).dt.normalize()
    turnover = (
        chinext.set_index("Date")["TurnoverRateFreeFloat"]
        .astype(float)
        .sort_index()
        .reindex(sessions)
    )
    output["risk_chinext_turnover_z20"] = (
        turnover - turnover.rolling(20).mean()
    ) / turnover.rolling(20).std(ddof=0)

    shares = inputs[_SHARES].drop_duplicates("Date", keep="last").copy()
    shares["Date"] = pd.to_datetime(shares["Date"]).dt.normalize()
    total_share = (
        shares.set_index("Date")["TotalShare"].astype(float).sort_index().reindex(sessions)
    )
    output["micro_share_change_5d_lag1"] = total_share.pct_change(5, fill_method=None).shift(1)

    spx = inputs[_SPX].drop_duplicates("Date", keep="last").copy()
    spx["Date"] = pd.to_datetime(spx["Date"]).dt.normalize()
    mapped = pd.merge_asof(
        pd.DataFrame({"date": sessions}),
        spx[["Date", "PercentChange"]].sort_values("Date"),
        left_on="date",
        right_on="Date",
        direction="backward",
        allow_exact_matches=False,
    ).set_index("date")
    output["risk_global_spx_return"] = mapped["PercentChange"].astype(float)
    # S007-v1 was researched and frozen with a feature panel beginning on this
    # session.  Earlier market rows may be published as calculation context,
    # but admitting them into rolling normalization changes the frozen target
    # sequence during 2021.  Keep the historical initialization boundary part
    # of the executable version identity.
    return output.loc[output.index >= _FROZEN_HISTORY_START]


def calculate_s007_history(
    panel: pd.DataFrame, normalization: Mapping[str, Any], score: Mapping[str, Any]
) -> pd.DataFrame:
    orientations = {
        name: int(value) for name, value in _object(score["orientations"], "orientations").items()
    }
    normalized = pd.DataFrame(index=panel.index)
    for feature, orientation in orientations.items():
        normalized[feature] = (
            causal_percentile(
                panel[feature],
                int(normalization["lookback_sessions"]),
                int(normalization["minimum_observations"]),
            )
            * orientation
        )
    base_weights = dict(_object(score["base_weights"], "base weights"))
    confirmation_weights = dict(_object(score["confirmation_weights"], "confirmation weights"))
    base = normalized.mul(pd.Series(base_weights), axis=1).sum(axis=1, min_count=len(base_weights))
    confirmation = normalized.mul(pd.Series(confirmation_weights), axis=1).sum(
        axis=1, min_count=len(confirmation_weights)
    )
    current = 0
    targets: list[int] = []
    actions: list[str] = []
    for base_value, confirmation_value in zip(base, confirmation, strict=True):
        action = "HOLD_POSITION" if current else "HOLD_CASH"
        if np.isfinite(base_value):
            if (
                current == 0
                and base_value >= float(score["entry_threshold"])
                and confirmation_value >= float(score["confirmation_threshold"])
            ):
                current = 1
                action = "ENTER"
            elif current == 1 and base_value <= float(score["exit_threshold"]):
                current = 0
                action = "EXIT"
        targets.append(current)
        actions.append(action)
    return pd.DataFrame(
        {
            "base_score": base,
            "confirmation_score": confirmation,
            "target_position": targets,
            "action": actions,
        },
        index=panel.index,
    )


def resolve_s007_feature_panel(
    inputs: Mapping[str, pd.DataFrame],
    score: Mapping[str, Any],
    sessions: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Combine immutable development evidence with post-cutoff DFLS features."""

    features = sorted(score["orientations"])
    evidence = inputs.get(_EVIDENCE)
    if evidence is None:
        panel = materialize_s007_features(inputs)
    else:
        frozen = evidence.copy()
        date_column = "Date" if "Date" in frozen else "date"
        frozen[date_column] = pd.to_datetime(frozen[date_column]).dt.normalize()
        frozen = frozen.set_index(date_column).sort_index().loc[:, features]
        if sessions.max() <= frozen.index.max():
            panel = frozen
        else:
            materialized = materialize_s007_features(inputs)
            additions = materialized.loc[materialized.index > frozen.index.max(), features]
            panel = pd.concat([frozen, additions])
    return panel[~panel.index.duplicated(keep="last")].sort_index().reindex(sessions)


class S007V1:
    """Executable S007-v1; every feature is materialized from DFLS inputs."""

    def __init__(self, release: StrategyRelease) -> None:
        payload = _object(release.payload, "strategy payload")
        if payload.get("strategy_kind") != "causal_feature_gate":
            raise RuntimeContractError("S007-v1 strategy_kind differs")
        rule = _object(payload.get("rule"), "S007-v1 rule")
        self._normalization = _object(rule.get("normalization"), "S007-v1 normalization")
        self._score = _object(rule.get("score"), "S007-v1 score")
        execution = _object(rule.get("execution"), "S007-v1 execution")
        self._symbol = str(rule.get("symbol", "")).upper()
        if self._symbol != "588080.SH":
            raise RuntimeContractError("S007-v1 frozen symbol must be 588080.SH")
        self._release = release
        requirements = (
            InputRequirement(
                _MARKET,
                Dataset.ETF_OHLCV.value,
                self._symbol,
                "daily",
                252,
                CutoffRule.SIGNAL_SESSION,
            ),
            InputRequirement(
                _SHIBOR, Dataset.SHIBOR_DAILY.value, None, "daily", 252, CutoffRule.SIGNAL_SESSION
            ),
            InputRequirement(
                _CHINEXT,
                Dataset.INDEX_DAILY_BASIC.value,
                "399006.SZ",
                "daily",
                252,
                CutoffRule.SIGNAL_SESSION,
            ),
            InputRequirement(
                _SHARES,
                Dataset.ETF_SHARE_SIZE.value,
                self._symbol,
                "daily",
                6,
                CutoffRule.PREVIOUS_SESSION,
            ),
            InputRequirement(
                _SPX,
                Dataset.GLOBAL_INDEX_DAILY.value,
                "SPX",
                "daily",
                252,
                CutoffRule.LATEST_AVAILABLE,
                7,
            ),
            InputRequirement(
                _EXECUTION,
                Dataset.ETF_UNADJUSTED_DAILY.value,
                self._symbol,
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
        self._definition = RuntimeDefinition(
            1,
            release.strategy_family_id,
            release.version,
            release.release_id,
            release.release_hash,
            ImplementationRef(
                __name__,
                self.__class__.__name__,
                1,
                implementation_sha256(("strategies/s007_v1.py", "execution_rules.py")),
            ),
            ParameterSet(release.payload),
            InputContract(requirements),
            DecisionContract("TARGET_POSITION", 0.0, 1.0, "NEXT_SESSION_OPEN"),
            ExecutionPolicy("FROZEN_RULE", execution),
            MonitoringPolicy("FORWARD_OBSERVATION", {"frozen": True}),
            RequiredCapabilities(
                tuple(sorted({item.dataset for item in requirements})),
                tuple(sorted({
                    effective_target_order_type(execution, "BUY"),
                    effective_target_order_type(execution, "SELL"),
                })),
            ),
            history=HistoryPolicy("CANONICAL_REPLAY", "2021-01-04", "2020-12-01"),
        )

    @classmethod
    def from_release(cls, release: StrategyRelease) -> "S007V1":
        if release.release_id != "S007-v1":
            raise RuntimeContractError("S007V1 can only load S007-v1")
        return cls(release)

    @property
    def definition(self) -> RuntimeDefinition:
        return self._definition

    def calculate_history(
        self,
        inputs: Mapping[str, pd.DataFrame],
        sessions: pd.DatetimeIndex,
    ) -> pd.DataFrame:
        panel = resolve_s007_feature_panel(inputs, self._score, sessions)
        return calculate_s007_history(panel, self._normalization, self._score)

    def publish_data(
        self, dataflows: Dataflows, deployment: DeploymentSpec, through: datetime
    ) -> PublishedStrategyData:
        cutoff = through.astimezone(_SHANGHAI).date()
        start = _FROZEN_HISTORY_START.date()
        calendar_end = cutoff + timedelta(days=20)
        options: dict[str, object] = {}
        if deployment.settings.get("env_file") is not None:
            options["env_file"] = str(deployment.settings["env_file"])
        calendar_request = DataRequest(
            Dataset.TRADING_CALENDAR,
            "SSE",
            start.isoformat(),
            calendar_end.isoformat(),
            calendar_end.isoformat(),
            "daily",
            options,
        )
        calendar_result = dataflows.fetch(calendar_request)
        requests: dict[str, DataRequest] = {_CALENDAR: calendar_request}
        results: dict[str, DataResult] = {_CALENDAR: calendar_result}
        if calendar_result.ready:
            dates = pd.to_datetime(
                calendar_result.dataframe.loc[
                    calendar_result.dataframe["IsOpen"].astype(int).eq(1), "Date"
                ]
            ).dt.normalize()
            previous = dates[dates < pd.Timestamp(cutoff)]
            if previous.empty:
                raise RuntimeContractError("S007-v1 calendar has no previous session")
            previous_date = previous.iloc[-1].date().isoformat()
            specs = {
                _MARKET: (Dataset.ETF_OHLCV, self._symbol, cutoff.isoformat(), "daily"),
                _SHIBOR: (Dataset.SHIBOR_DAILY, None, cutoff.isoformat(), "daily"),
                _CHINEXT: (Dataset.INDEX_DAILY_BASIC, "399006.SZ", cutoff.isoformat(), "daily"),
                _SHARES: (Dataset.ETF_SHARE_SIZE, self._symbol, previous_date, "daily"),
                _SPX: (Dataset.GLOBAL_INDEX_DAILY, "SPX", None, "daily"),
                _EXECUTION: (
                    Dataset.ETF_UNADJUSTED_DAILY,
                    self._symbol,
                    cutoff.isoformat(),
                    "daily",
                ),
            }
            for name, (dataset, symbol, required, frequency) in specs.items():
                request_end = previous_date if name == _SHARES else cutoff.isoformat()
                request_start = (
                    _GLOBAL_HISTORY_START.date().isoformat()
                    if name == _SPX
                    else start.isoformat()
                )
                requests[name] = DataRequest(
                    dataset,
                    symbol,
                    (
                        cutoff.isoformat()
                        if name == _EXECUTION
                        else request_start
                    ),
                    request_end,
                    required,
                    frequency,
                    options,
                )
                results[name] = dataflows.fetch(requests[name])
        else:
            for requirement in self._definition.inputs.requirements:
                if requirement.name == _CALENDAR:
                    continue
                requests[requirement.name] = DataRequest(
                    requirement.dataset,
                    requirement.subject,
                    start.isoformat(),
                    cutoff.isoformat(),
                    None,
                    requirement.frequency,
                    options,
                )
                results[requirement.name] = DataResult(
                    DataStatus.INCOMPLETE, error=calendar_result.error
                )
        status = _status(results)
        error = (
            None
            if status is PublicationStatus.READY
            else "; ".join(
                f"{name}:{result.status.value}"
                for name, result in results.items()
                if not result.ready
            )
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
        frames = {
            name: result.dataframe for name, result in request.publication.input_results.items()
        }
        sessions = pd.DatetimeIndex(
            pd.to_datetime(frames[_MARKET]["Date"]).dt.normalize().sort_values().unique()
        )
        panel = resolve_s007_feature_panel(frames, self._score, sessions)
        history = calculate_s007_history(panel, self._normalization, self._score)
        cutoff = pd.Timestamp(request.publication.requested_cutoff).normalize()
        if (
            cutoff not in history.index
            or pd.isna(history.loc[cutoff, "base_score"])
            or pd.isna(history.loc[cutoff, "confirmation_score"])
        ):
            raise RuntimeContractError("S007-v1 feature history does not reach a valid cutoff")
        row = history.loc[cutoff]
        calendar = frames[_CALENDAR]
        dates = pd.to_datetime(
            calendar.loc[calendar["IsOpen"].astype(int).eq(1), "Date"]
        ).dt.normalize()
        future = dates[dates > cutoff]
        if future.empty:
            raise RuntimeContractError("S007-v1 calendar has no next session")
        valid_at = datetime.combine(future.iloc[0].date(), time(9, 30), tzinfo=_SHANGHAI)
        execution = frames[_EXECUTION]
        execution_row = execution.loc[pd.to_datetime(execution["Date"]).dt.normalize().eq(cutoff)]
        if len(execution_row) != 1:
            raise RuntimeContractError("S007-v1 requires one execution-price row")
        identities = {
            name: result.identity.content_sha256
            for name, result in request.publication.input_results.items()
        }
        suffix = canonical_sha256(
            {
                "release": self._release.release_hash,
                "cutoff": cutoff.date().isoformat(),
                "target": float(row["target_position"]),
                "inputs": identities,
            }
        )[:12].upper()
        evidence = {
            "signal_date": cutoff.date().isoformat(),
            "base_score": float(row["base_score"]),
            "confirmation_score": float(row["confirmation_score"]),
            "action": str(row["action"]),
            "execution_reference_price": float(execution_row.iloc[0]["Close"]),
            "features": {
                name: float(panel.loc[cutoff, name]) for name in sorted(self._score["orientations"])
            },
        }
        return StrategyDecision(
            f"DEC-{request.calculation_time.astimezone(_SHANGHAI):%Y%m%d-%H%M}-{suffix}",
            request.deployment.deployment_id,
            self._release.release_id,
            self._release.release_hash,
            self._definition.runtime_sha256,
            request.calculation_time,
            valid_at,
            float(row["target_position"]),
            request.account.revision,
            request.state.revision,
            identities,
            evidence,
            {
                "signal_date": cutoff.date().isoformat(),
                "target_position": float(row["target_position"]),
                "action": str(row["action"]),
            },
        )

    def explain(self, decision: StrategyDecision) -> StrategyExplanation:
        return StrategyExplanation(
            f"S007-v1 由基础分与确认门共同给出 {decision.evidence['action']}",
            (
                f"基础分：{decision.evidence['base_score']:.4f}",
                f"确认分：{decision.evidence['confirmation_score']:.4f}",
            ),
            ("跨市场与资金状态的历史关系可能随市场结构变化而衰减",),
            MappingProxyType(dict(decision.evidence)),
        )
