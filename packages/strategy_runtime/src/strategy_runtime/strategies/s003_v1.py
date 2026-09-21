"""Frozen S003-v1 constituent-moneyflow intraday overlay runtime."""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

import pandas as pd
from dataflows import Dataset

from ..algorithm import StrategyImplementation
from ..calculation import (
    CalculationScope,
    CalendarWindow,
    next_session_calculation_scope,
    next_session_calendar_window,
)
from ..contracts import TradableWindow
from ..errors import RuntimeContractError
from ..implementation_identity import implementation_sha256
from ..models import (
    CutoffRule,
    DecisionContract,
    ExecutionPolicy,
    ImplementationRef,
    InputContract,
    InputRequirement,
    MonitoringPolicy,
    ParameterSet,
    RequiredCapabilities,
    RuntimeDefinition,
    StrategyRelease,
)


_WEIGHTS = "constituent_weights"
_MONEYFLOW = "constituent_moneyflow"
_MARKET = "adjusted_daily"
_EXECUTION = "execution_daily"
_CALENDAR = "trading_calendar"
_INDEX_SYMBOL = "000905.SH"


def _object(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeContractError(f"{field_name} must be an object")
    return value


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


class S003V1(StrategyImplementation):
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
                implementation_sha256(
                    ("strategies/s003_v1.py", "calculation.py", "execution_rules.py")
                ),
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
                        _MARKET,
                        Dataset.ETF_OHLCV.value,
                        self._symbol,
                        "daily",
                        1,
                        CutoffRule.SIGNAL_SESSION,
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
            ),
            DecisionContract("INTRADAY_OVERLAY", 0.0, 1.0, "NEXT_SESSION_OPEN_TO_11_30"),
            ExecutionPolicy("INTRADAY_OVERLAY", execution),
            MonitoringPolicy("FORWARD_OBSERVATION", {"frozen": True}),
            RequiredCapabilities(
                (
                    Dataset.INDEX_CONSTITUENT_WEIGHT.value,
                    Dataset.STOCK_MONEYFLOW.value,
                    Dataset.ETF_OHLCV.value,
                    Dataset.ETF_UNADJUSTED_DAILY.value,
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

    def calendar_window(self, tradable_window: TradableWindow) -> CalendarWindow:
        return next_session_calendar_window(self._definition, tradable_window)

    def derive_calculation_scope(
        self,
        tradable_window: TradableWindow,
        calendar_dates: tuple[date, ...],
    ) -> CalculationScope:
        return next_session_calculation_scope(
            self._definition,
            tradable_window,
            calendar_dates,
        )

    def calculate_history(
        self,
        inputs: Mapping[str, pd.DataFrame],
        sessions: pd.DatetimeIndex,
    ) -> pd.DataFrame:
        if "strategy_evidence" in inputs:
            history = calculate_s003_evidence_history(inputs["strategy_evidence"], self._feature)
            return history.reindex(sessions)
        return calculate_s003_history(inputs[_WEIGHTS], inputs[_MONEYFLOW], sessions, self._feature)
