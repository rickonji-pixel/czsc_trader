"""Frozen S003-v1 constituent-moneyflow intraday overlay runtime."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from types import MappingProxyType
from typing import Any, Mapping
from zoneinfo import ZoneInfo

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
_WEIGHTS = "constituent_weights"
_MONEYFLOW = "constituent_moneyflow"
_CALENDAR = "trading_calendar"
_INDEX_SYMBOL = "000905.SH"


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


def calculate_s003_history(
    weights: pd.DataFrame,
    moneyflow: pd.DataFrame,
    sessions: pd.DatetimeIndex,
    feature: Mapping[str, Any],
) -> pd.DataFrame:
    """Build the complete causal breadth and decision history."""

    snapshots = weights.rename(
        columns={"Date": "snapshot_date", "ConstituentSymbol": "symbol", "Weight": "weight"}
    ).copy()
    snapshots["snapshot_date"] = pd.to_datetime(snapshots["snapshot_date"]).dt.normalize()
    snapshots["weight"] = pd.to_numeric(snapshots["weight"], errors="raise")
    snapshots = snapshots.sort_values(["snapshot_date", "symbol"])
    if snapshots.duplicated(["snapshot_date", "symbol"]).any():
        raise RuntimeContractError("S003-v1 constituent weights contain duplicate members")

    flows = moneyflow.rename(
        columns={"Date": "date", "Symbol": "symbol", "NetMoneyflowAmount": "net_moneyflow"}
    ).copy()
    flows["date"] = pd.to_datetime(flows["date"]).dt.normalize()
    flows["net_moneyflow"] = pd.to_numeric(flows["net_moneyflow"], errors="coerce")
    if flows.duplicated(["date", "symbol"]).any():
        raise RuntimeContractError("S003-v1 moneyflow contains duplicate member sessions")

    rows: list[dict[str, object]] = []
    minimum = float(feature["minimum_observed_weight_ratio"])
    for session in pd.DatetimeIndex(sessions).normalize().unique().sort_values():
        # The frozen research contract only permits a constituent snapshot
        # strictly earlier than the signal session.
        eligible = snapshots.loc[snapshots["snapshot_date"].lt(session), "snapshot_date"]
        if eligible.empty:
            continue
        snapshot_date = eligible.max()
        members = snapshots.loc[snapshots["snapshot_date"].eq(snapshot_date), ["symbol", "weight"]]
        selected = members.merge(
            flows.loc[flows["date"].eq(session), ["symbol", "net_moneyflow"]],
            on="symbol",
            how="left",
            validate="one_to_one",
        )
        total = float(selected["weight"].sum())
        observed = float(selected.loc[selected["net_moneyflow"].notna(), "weight"].sum())
        positive = float(selected.loc[selected["net_moneyflow"].gt(0), "weight"].sum())
        coverage = observed / total if total else float("nan")
        breadth = positive / observed if observed and coverage >= minimum else float("nan")
        rows.append(
            {
                "date": session,
                "snapshot_date": snapshot_date,
                "observed_weight_ratio": coverage,
                "moneyflow_breadth": breadth,
            }
        )
    daily = pd.DataFrame(rows).set_index("date").sort_index()
    lookback = int(feature["threshold_lookback_sessions"])
    quantile = float(feature["threshold_quantile"])
    source = (
        daily["moneyflow_breadth"].shift(1)
        if feature["threshold_excludes_current_session"]
        else daily["moneyflow_breadth"]
    )
    daily["threshold"] = source.rolling(lookback, min_periods=lookback).quantile(quantile)
    daily["signal_active"] = daily["moneyflow_breadth"].ge(daily["threshold"])
    daily["target_position"] = daily["signal_active"].astype(float)
    return daily


def calculate_s003_evidence_history(
    panel: pd.DataFrame, feature: Mapping[str, Any]
) -> pd.DataFrame:
    """Replay the accepted panel with its original tradable-member filter intact."""

    source = panel.copy()
    source["dt"] = pd.to_datetime(source["dt"]).dt.normalize()
    source["weight"] = pd.to_numeric(source["weight"], errors="raise")
    source["net_mf_amount"] = pd.to_numeric(source["net_mf_amount"], errors="coerce")
    observed = (
        source["observed_moneyflow"]
        .astype(str)
        .str.lower()
        .map({"true": True, "false": False, "1": True, "0": False})
    )
    if observed.isna().any():
        raise RuntimeContractError("S003-v1 evidence has invalid observed flags")
    source["observed_weight"] = source["weight"].where(observed, 0.0)
    source["positive_weight"] = source["weight"].where(source["net_mf_amount"].gt(0), 0.0)
    daily = source.groupby("dt", sort=True, observed=True).agg(
        total_weight=("weight", "sum"),
        observed_weight=("observed_weight", "sum"),
        positive_weight=("positive_weight", "sum"),
    )
    daily.index.name = "date"
    daily["observed_weight_ratio"] = daily["observed_weight"] / daily["total_weight"]
    daily["moneyflow_breadth"] = daily["positive_weight"] / daily["observed_weight"]
    daily.loc[
        daily["observed_weight_ratio"].lt(float(feature["minimum_observed_weight_ratio"])),
        "moneyflow_breadth",
    ] = pd.NA
    values = daily["moneyflow_breadth"]
    if feature["threshold_excludes_current_session"]:
        values = values.shift(1)
    lookback = int(feature["threshold_lookback_sessions"])
    daily["threshold"] = values.rolling(lookback, min_periods=lookback).quantile(
        float(feature["threshold_quantile"])
    )
    daily["signal_active"] = daily["moneyflow_breadth"].ge(daily["threshold"])
    daily["target_position"] = daily["signal_active"].astype(float)
    return daily


class S003V1:
    """Executable S003-v1 with DFLS-owned raw inputs and no TDR dependency."""

    def __init__(self, release: StrategyRelease) -> None:
        payload = _object(release.payload, "strategy payload")
        if payload.get("strategy_kind") != "constituent_moneyflow_intraday_overlay":
            raise RuntimeContractError("S003-v1 strategy_kind differs")
        rule = _object(payload.get("rule"), "S003-v1 rule")
        self._feature = _object(rule.get("feature"), "S003-v1 feature")
        execution = _object(rule.get("execution"), "S003-v1 execution")
        self._symbol = str(rule.get("symbol", "")).upper()
        if self._symbol != "510500.SH":
            raise RuntimeContractError("S003-v1 frozen symbol must be 510500.SH")
        self._release = release
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
                implementation_sha256(("strategies/s003_v1.py", "execution_planner.py")),
            ),
            ParameterSet(release.payload),
            InputContract(
                (
                    InputRequirement(
                        _WEIGHTS,
                        Dataset.INDEX_CONSTITUENT_WEIGHT.value,
                        _INDEX_SYMBOL,
                        "snapshot",
                        1,
                        CutoffRule.LATEST_AVAILABLE,
                        370,
                    ),
                    InputRequirement(
                        _MONEYFLOW,
                        Dataset.STOCK_MONEYFLOW.value,
                        None,
                        "daily",
                        60,
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
            DecisionContract("INTRADAY_OVERLAY", 0.0, 1.0, "NEXT_SESSION_OPEN_TO_11_30"),
            ExecutionPolicy("INTRADAY_OVERLAY", execution),
            MonitoringPolicy("FORWARD_OBSERVATION", {"frozen": True}),
            RequiredCapabilities(
                (
                    Dataset.INDEX_CONSTITUENT_WEIGHT.value,
                    Dataset.STOCK_MONEYFLOW.value,
                    Dataset.TRADING_CALENDAR.value,
                ),
                ("LIMIT", "MARKET"),
                ("OPEN", "11:30_CLOSE"),
            ),
        )

    @classmethod
    def from_release(cls, release: StrategyRelease) -> "S003V1":
        if release.release_id != "S003-v1":
            raise RuntimeContractError("S003V1 can only load S003-v1")
        return cls(release)

    @property
    def definition(self) -> RuntimeDefinition:
        return self._definition

    def calculate_history(
        self,
        inputs: Mapping[str, pd.DataFrame],
        sessions: pd.DatetimeIndex,
    ) -> pd.DataFrame:
        if "strategy_evidence" in inputs:
            history = calculate_s003_evidence_history(inputs["strategy_evidence"], self._feature)
            return history.reindex(sessions)
        return calculate_s003_history(inputs[_WEIGHTS], inputs[_MONEYFLOW], sessions, self._feature)

    def publish_data(
        self, dataflows: Dataflows, deployment: DeploymentSpec, through: datetime
    ) -> PublishedStrategyData:
        cutoff = through.astimezone(_SHANGHAI).date()
        start = cutoff - timedelta(days=180)
        options: dict[str, object] = {}
        if deployment.settings.get("env_file") is not None:
            options["env_file"] = str(deployment.settings["env_file"])
        calendar_end = cutoff + timedelta(days=20)
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
            calendar = calendar_result.dataframe
            open_dates = pd.to_datetime(
                calendar.loc[calendar["IsOpen"].astype(int).eq(1), "Date"]
            ).dt.normalize()
            open_dates = open_dates[open_dates <= pd.Timestamp(cutoff)]
            dates = tuple(item.date().isoformat() for item in open_dates)
            requests[_WEIGHTS] = DataRequest(
                Dataset.INDEX_CONSTITUENT_WEIGHT,
                _INDEX_SYMBOL,
                (cutoff - timedelta(days=550)).isoformat(),
                cutoff.isoformat(),
                None,
                "snapshot",
                options,
            )
            requests[_MONEYFLOW] = DataRequest(
                Dataset.STOCK_MONEYFLOW,
                None,
                start.isoformat(),
                cutoff.isoformat(),
                cutoff.isoformat(),
                "daily",
                {**options, "trading_dates": dates},
            )
            results[_WEIGHTS] = dataflows.fetch(requests[_WEIGHTS])
            results[_MONEYFLOW] = dataflows.fetch(requests[_MONEYFLOW])
        else:
            for name, dataset, symbol, frequency in (
                (_WEIGHTS, Dataset.INDEX_CONSTITUENT_WEIGHT, _INDEX_SYMBOL, "snapshot"),
                (_MONEYFLOW, Dataset.STOCK_MONEYFLOW, None, "daily"),
            ):
                requests[name] = DataRequest(
                    dataset, symbol, start.isoformat(), cutoff.isoformat(), None, frequency, options
                )
                results[name] = DataResult(DataStatus.INCOMPLETE, error=calendar_result.error)
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
            release_id=self._release.release_id,
            release_hash=self._release.release_hash,
            status=status,
            requested_cutoff=cutoff.isoformat(),
            input_requests=requests,
            input_results=results,
            error=error,
        )

    def calculate(self, request: CalculationRequest) -> StrategyDecision:
        weights = request.publication.input_results[_WEIGHTS].dataframe
        moneyflow = request.publication.input_results[_MONEYFLOW].dataframe
        calendar = request.publication.input_results[_CALENDAR].dataframe
        sessions = pd.DatetimeIndex(
            pd.to_datetime(calendar.loc[calendar["IsOpen"].astype(int).eq(1), "Date"])
        ).normalize()
        history = calculate_s003_history(weights, moneyflow, sessions, self._feature)
        cutoff = pd.Timestamp(request.publication.requested_cutoff).normalize()
        if cutoff not in history.index or pd.isna(history.loc[cutoff, "threshold"]):
            raise RuntimeContractError("S003-v1 history does not reach a valid cutoff")
        row = history.loc[cutoff]
        future = sessions[sessions > cutoff]
        if future.empty:
            raise RuntimeContractError("S003-v1 calendar has no next session")
        valid_at = datetime.combine(future[0].date(), time(9, 30), tzinfo=_SHANGHAI)
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
            "moneyflow_breadth": float(row["moneyflow_breadth"]),
            "threshold": float(row["threshold"]),
            "observed_weight_ratio": float(row["observed_weight_ratio"]),
            "snapshot_date": pd.Timestamp(row["snapshot_date"]).date().isoformat(),
            "action": "INTRADAY_LONG_OVERLAY" if row["signal_active"] else "NO_EVENT",
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
            {"signal_date": cutoff.date().isoformat(), "event_active": bool(row["signal_active"])},
        )

    def explain(self, decision: StrategyDecision) -> StrategyExplanation:
        return StrategyExplanation(
            "S003-v1 根据成分股资金流宽度决定次日早盘是否启用事件仓位",
            (
                f"资金流宽度：{decision.evidence['moneyflow_breadth']:.2%}",
                f"门槛：{decision.evidence['threshold']:.2%}",
            ),
            ("成分资金流的早盘延续效应可能衰减",),
            MappingProxyType(dict(decision.evidence)),
        )
