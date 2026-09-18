from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd
from strategy_evaluator import ReplayEvidence

from .datasets import ReplayData
from .result import BacktestResult
from .signal_replay import SignalReplay


def _records(frame: pd.DataFrame, date_columns: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
    value = frame.copy()
    for column in date_columns:
        if column in value:
            value[column] = value[column].map(
                lambda item: None if pd.isna(item) else pd.Timestamp(item).isoformat()
            )
    value = value.replace({float("nan"): None})
    return tuple(value.to_dict(orient="records"))


def build_replay_evidence(
    signals: SignalReplay,
    data: ReplayData,
    result: BacktestResult,
    initial_cash: float,
    metrics: dict[str, object],
) -> ReplayEvidence:
    support = signals.support_data or {}
    srt_policy = support.get("execution_policy")
    spec = None
    if support.get("mode") == "srt_input_contract":
        if not isinstance(srt_policy, dict):
            raise ValueError("SRT replay has no execution policy evidence")
        policy_type = srt_policy.get("policy_type")
        settings = srt_policy.get("settings")
        if not isinstance(settings, dict):
            raise ValueError("SRT replay execution settings are invalid")
        overlay_settings = settings if policy_type == "INTRADAY_OVERLAY" else None
        frozen_settings = settings if policy_type == "FROZEN_RULE" else None
        if overlay_settings is None and frozen_settings is None:
            raise ValueError(f"unsupported SRT execution policy: {policy_type}")
    else:
        resolved = signals.snapshot.resolved_rule
        if resolved is None:
            raise ValueError("candidate replay requires a resolved research rule")
        spec = resolved.execution
        overlay = resolved.constituent_moneyflow_intraday
        overlay_settings = (
            {
                "one_way_cost": overlay.one_way_cost,
                "lot_size": overlay.lot_size,
                "core_fraction": overlay.core_fraction,
                "event_fraction": overlay.event_fraction,
                "entry_checkpoint": overlay.entry_checkpoint,
                "exit_checkpoint": overlay.exit_checkpoint,
                "t_plus_one_inventory_rotation": overlay.t_plus_one_inventory_rotation,
            }
            if overlay is not None
            else None
        )
        frozen_settings = None
    if overlay_settings is not None:
        if data.execution_five_minute is None:
            raise ValueError("intraday overlay audit requires 5m execution data")
        daily = data.execution_daily.loc[
            data.execution_daily["dt"].le(signals.evaluation_end)
        ].rename(columns={"dt": "date"})
        intraday = data.execution_five_minute.loc[
            data.execution_five_minute["dt"].between(
                signals.evaluation_start,
                signals.evaluation_end + pd.Timedelta(days=1),
            )
        ].rename(columns={"dt": "time"})
        return ReplayEvidence(
            strategy_hash=signals.snapshot.content_hash,
            data_hash=data.fingerprint,
            initial_cash=float(initial_cash),
            execution_spec={
                "mode": "CORE_EVENT_INTRADAY_ROTATION",
                "fee_rate": overlay_settings["one_way_cost"],
                "lot_size": overlay_settings["lot_size"],
                "core_fraction": overlay_settings["core_fraction"],
                "event_fraction": overlay_settings["event_fraction"],
                "entry_checkpoint": overlay_settings["entry_checkpoint"],
                "exit_checkpoint": overlay_settings["exit_checkpoint"],
                "t_plus_one_inventory_rotation": overlay_settings[
                    "t_plus_one_inventory_rotation"
                ],
                "order_semantics": (
                    "SRT_PLAN"
                    if support.get("mode") == "srt_input_contract"
                    else "LEGACY_REPLAY"
                ),
            },
            decisions=_records(result.decisions, ("signal_date", "valid_session")),
            orders=_records(result.orders, ("signal_date", "execution_date")),
            fills=_records(result.fills, ("signal_date", "fill_time")),
            account_daily=_records(result.account_daily, ("date", "signal_date")),
            trades=_records(result.trades, ("entry_date", "exit_date")),
            metrics=metrics,
            execution_daily=_records(daily, ("date",)),
            execution_intraday=_records(intraday, ("time",)),
        )
    if frozen_settings is not None:
        entry = frozen_settings.get("entry")
        exit_rule = frozen_settings.get("exit")
        capital = frozen_settings.get("capital")
        instrument = frozen_settings.get("instrument")
        if not all(
            isinstance(item, Mapping)
            for item in (entry, exit_rule, capital, instrument)
        ):
            raise ValueError("SRT frozen execution policy is structurally incomplete")
        entry_order_type = support.get("entry_order_type")
        exit_order_type = support.get("exit_order_type")
        if not all(
            isinstance(item, str) and item.strip()
            for item in (entry_order_type, exit_order_type)
        ):
            raise ValueError("SRT replay has no effective order-type evidence")
        execution_spec = {
            "entry_limit_parameter": entry["limit_parameter"],
            "exit_limit_ratio": exit_rule["limit_ratio"],
            "entry_order_type": entry_order_type,
            "exit_order_type": exit_order_type,
            "fee_rate": capital["fee_rate"],
            "capital_mode": capital["mode"],
            "allocation_fraction": capital.get("allocation_fraction", 1.0),
            "instrument": {
                "lot_size": instrument["lot_size"],
                "price_tick": instrument["price_tick"],
                "price_limit_ratio": instrument["price_limit_ratio"],
                "maximum_order_quantity": instrument["maximum_order_quantity"],
            },
        }
    else:
        if spec is None:
            raise ValueError("strategy snapshot has no execution specification")
        execution_spec = {
            "entry_limit_parameter": spec.entry_limit_parameter,
            "exit_limit_ratio": spec.exit_limit_ratio,
            "entry_order_type": str(spec.entry_order_type),
            "exit_order_type": "MARKET",
            "fee_rate": spec.capital.fee_rate,
            "capital_mode": spec.capital.mode,
            "allocation_fraction": spec.capital.allocation_fraction,
            "instrument": {
                "lot_size": spec.instrument.lot_size,
                "price_tick": spec.instrument.price_tick,
                "price_limit_ratio": spec.instrument.price_limit_ratio,
                "maximum_order_quantity": spec.instrument.maximum_order_quantity,
            },
        }
    start = signals.calculation_start
    end = signals.evaluation_end
    daily = data.execution_daily.loc[data.execution_daily["dt"].between(start, end)].rename(
        columns={"dt": "date"}
    )
    intraday = data.execution_intraday.loc[
        data.execution_intraday["dt"].between(start, end + pd.Timedelta(days=1))
    ].rename(columns={"dt": "time"})
    return ReplayEvidence(
        strategy_hash=signals.snapshot.content_hash,
        data_hash=data.fingerprint,
        initial_cash=float(initial_cash),
        execution_spec=execution_spec,
        decisions=_records(result.decisions, ("signal_date", "valid_session")),
        orders=_records(result.orders, ("signal_date", "execution_date")),
        fills=_records(result.fills, ("signal_date", "fill_time")),
        account_daily=_records(result.account_daily, ("date", "signal_date")),
        trades=_records(result.trades, ("entry_date", "exit_date")),
        metrics=metrics,
        execution_daily=_records(daily, ("date",)),
        execution_intraday=_records(intraday, ("time",)),
    )
