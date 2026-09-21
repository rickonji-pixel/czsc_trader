"""Caller-neutral strategy instance and its planning operations."""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from zoneinfo import ZoneInfo

import pandas as pd
from dataflows import Dataset

from .contracts import (
    DataPreparationResult,
    ExecutionCapabilities,
    ExecutionPlan,
    ExecutionState,
    OrderSide,
    OrderType,
    PlanLeg,
    PlannedOrder,
    PortfolioSnapshot,
    StrategyIdentity,
    TradingPoint,
    TradableWindow,
    WindowExecutor,
    plan_identity_for,
    signal_identity_for,
)
from .data import PreparedStrategyData
from .errors import RuntimeCompatibilityError, RuntimeContractError
from .execution_planner import build_execution_plan
from .models import ExecutionPricingData
from .preparation import PreparedInputs, prepare_inputs
from .prepared_store import load_prepared_inputs, save_prepared_inputs
from .signals import StrategySignal


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _planned_order(value: dict[str, object]) -> PlannedOrder:
    order_type = OrderType(str(value["order_type"]).upper())
    raw_price = value.get("limit_price")
    return PlannedOrder(
        side=OrderSide(str(value["side"]).upper()),
        quantity=int(value["quantity"]),
        order_type=order_type,
        limit_price=None if raw_price is None else Decimal(str(raw_price)),
    )


def _parse_time(value: object, name: str) -> time:
    try:
        return time.fromisoformat(str(value))
    except ValueError as exc:
        raise RuntimeContractError(f"{name} must use ISO local time") from exc


class StrategyInstance:
    """One immutable strategy identity, parameter set and tradable window."""

    def __init__(
        self,
        *,
        algorithm,
        identity: StrategyIdentity,
        tradable_window: TradableWindow,
        data_dir: Path,
        execution_policy,
    ) -> None:
        self._algorithm = algorithm
        self._identity = identity
        self._tradable_window = tradable_window
        self._data_dir = Path(data_dir).resolve()
        self._execution_policy = execution_policy
        self._prepared_data: PreparedStrategyData | None = None
        self._history_cache: tuple[str, pd.DataFrame, pd.DatetimeIndex] | None = None

    @property
    def identity(self) -> StrategyIdentity:
        return self._identity

    @property
    def tradable_window(self) -> TradableWindow:
        return self._tradable_window

    @property
    def definition(self):
        return self._algorithm.definition

    @property
    def execution_policy(self):
        return self._execution_policy

    def _pricing(self, inputs: PreparedInputs) -> ExecutionPricingData:
        adjusted = [
            result.dataframe
            for name, result in inputs.results.items()
            if str(inputs.requests[name].dataset) == Dataset.ETF_OHLCV.value
            and inputs.requests[name].frequency == "daily"
            and inputs.requests[name].symbol == self._identity.symbol
        ]
        execution = [
            result.dataframe
            for name, result in inputs.results.items()
            if str(inputs.requests[name].dataset) == Dataset.ETF_UNADJUSTED_DAILY.value
            and inputs.requests[name].frequency == "daily"
            and inputs.requests[name].symbol == self._identity.symbol
        ]
        if len(adjusted) != 1 or len(execution) != 1:
            raise RuntimeContractError(
                "strategy input contract must declare one adjusted and one execution price"
            )
        return ExecutionPricingData(
            self._identity.symbol,
            adjusted[0],
            execution[0],
        )

    def _summary(self, data: PreparedStrategyData) -> DataPreparationResult:
        return DataPreparationResult(
            self._identity,
            self._tradable_window,
            data.available_through,
            data.dataset_identity,
        )

    def prepare_data(self) -> DataPreparationResult:
        """Prepare and persist all calculation dependencies inside this instance."""

        if self._prepared_data is not None:
            return self._summary(self._prepared_data)
        inputs = load_prepared_inputs(
            self._data_dir,
            strategy=self._identity,
            tradable_window=self._tradable_window,
        )
        if inputs is None:
            inputs = prepare_inputs(
                strategy=self._identity,
                algorithm=self._algorithm,
                tradable_window=self._tradable_window,
                data_dir=self._data_dir,
            )
            pricing = self._pricing(inputs)
            prepared = PreparedStrategyData.from_inputs(
                inputs=inputs,
                pricing=pricing,
            )
            save_prepared_inputs(inputs, self._data_dir)
        else:
            prepared = PreparedStrategyData.from_inputs(
                inputs=inputs,
                pricing=self._pricing(inputs),
            )
        self._prepared_data = prepared
        return self._summary(prepared)

    def _prepared(self) -> PreparedStrategyData:
        if self._prepared_data is None:
            raise RuntimeContractError("strategy data is not prepared; call prepare_data() first")
        return self._prepared_data

    def _history(self, data: PreparedStrategyData) -> tuple[pd.DataFrame, pd.DatetimeIndex]:
        frames = {name: result.dataframe for name, result in data._inputs.results.items()}
        sessions = pd.DatetimeIndex(
            pd.to_datetime(data.calculation_dates(), errors="raise"), name="dt"
        ).normalize()
        cached = self._history_cache
        if cached is not None and cached[0] == data.dataset_identity:
            history, sessions = cached[1], cached[2]
        else:
            history = self._algorithm.calculate_history(frames, sessions)
            self._history_cache = (data.dataset_identity, history, sessions)
        return history, sessions

    def _calculate_signal(
        self,
        data: PreparedStrategyData,
        point: TradingPoint,
    ) -> StrategySignal:
        history, sessions = self._history(data)
        signal_date = data.signal_date_for(point.trading_date)
        signal_session = pd.Timestamp(signal_date).normalize()
        if signal_session not in history.index:
            raise RuntimeContractError(
                "strategy history does not reach the signal date: "
                f"signal={signal_date.isoformat()}, "
                f"range={history.index.min()}..{history.index.max()}"
            )
        row = history.loc[signal_session]
        target_position = float(row["target_position"])
        if not (
            self.definition.decision.minimum_target
            <= target_position
            <= self.definition.decision.maximum_target
        ):
            raise RuntimeContractError("strategy target position violates its contract")
        evidence: dict[str, object] = {"signal_date": signal_date.isoformat()}
        for name, value in row.items():
            if name == "target_position" or pd.isna(value):
                continue
            if isinstance(value, pd.Timestamp):
                evidence[str(name)] = value.date().isoformat()
            elif hasattr(value, "item"):
                evidence[str(name)] = value.item()
            else:
                evidence[str(name)] = value
        if "action" not in evidence:
            if bool(evidence.get("signal_active", False)):
                evidence["action"] = "INTRADAY_LONG_OVERLAY"
            elif self.definition.decision.output_kind == "INTRADAY_OVERLAY":
                evidence["action"] = "NO_EVENT"
            else:
                previous = history["target_position"].shift(1, fill_value=0.0).loc[signal_session]
                evidence["action"] = (
                    "BUY"
                    if target_position > float(previous)
                    else "SELL"
                    if target_position < float(previous)
                    else "HOLD"
                )
        return StrategySignal(
            trading_date=point.trading_date,
            target_position=target_position,
            evidence=evidence,
            next_state={
                "signal_date": signal_date.isoformat(),
                "target_position": target_position,
            },
        )

    def inspect_signals(self) -> pd.DataFrame:
        """Return a defensive copy of strategy signal history for diagnostics."""

        data = self._prepared()
        history, _ = self._history(data)
        return history.copy()

    def inspect_price_history(self) -> pd.DataFrame:
        """Return adjusted daily prices without exposing strategy input datasets."""

        return self._prepared().adjusted_daily

    def plan_at(
        self,
        *,
        point: TradingPoint,
        portfolio: PortfolioSnapshot,
        state: ExecutionState,
    ) -> ExecutionPlan:
        data = self._prepared()
        if not self._tradable_window.contains(point.trading_date):
            raise RuntimeContractError("trading point is outside the strategy window")
        if portfolio.symbol != self._identity.symbol:
            raise RuntimeContractError("portfolio symbol differs from strategy")
        if portfolio.as_of > point.calculation_time or state.as_of > point.calculation_time:
            raise RuntimeContractError("planning state is newer than calculation time")

        signal = self._calculate_signal(data, point)
        signal_date = data.signal_date_for(point.trading_date)
        if signal.evidence.get("signal_date") != signal_date.isoformat():
            raise RuntimeContractError("strategy signal evidence refers to another date")
        references = data.price_reference(signal_date, point.trading_date)
        raw = dict(
            build_execution_plan(
                deployment_settings={"cycle_target_quantity": state.cycle_target_quantity},
                available_cash=float(portfolio.available_cash),
                position_quantity=portfolio.position_quantity,
                target_position=signal.target_position,
                policy=self._execution_policy,
                signal_reference_price=float(references.signal_price),
                execution_reference_price=float(references.execution_price),
            )
        )
        orders = tuple(_planned_order(value) for value in raw.get("orders", ()))
        legs = tuple(
            PlanLeg(
                sequence=int(value["sequence"]),
                role=str(value["role"]),
                checkpoint=str(value["checkpoint"]),
                submit_after=_parse_time(value["submit_after"], "submit_after"),
                submit_before=_parse_time(value["submit_before"], "submit_before"),
                dependency_sequence=(
                    None
                    if value.get("dependency_sequence") is None
                    else int(value["dependency_sequence"])
                ),
                dependency_required_status=(
                    None
                    if value.get("dependency_required_status") is None
                    else str(value["dependency_required_status"])
                ),
                order=_planned_order(dict(value["order"])),
            )
            for value in raw.get("plan_legs", ())
        )
        required_orders = tuple(
            sorted(
                {order.order_type for order in orders} | {leg.order.order_type for leg in legs},
                key=lambda value: value.value,
            )
        )
        required_checkpoints = tuple(sorted({leg.checkpoint for leg in legs}))
        signal_identity = signal_identity_for(
            strategy=self._identity,
            signal_date=signal_date,
            target_position=signal.target_position,
            input_identities=data.input_identities,
            price_identities=data.price_identities,
        )
        cycle_target = int(raw["cycle_target_quantity"])
        target_quantity = int(raw["target_quantity"])
        plan_mode = str(raw.get("plan_mode", "NONE"))
        capital_rule = dict(raw["capital_rule"])
        capital_mode = str(capital_rule["mode"])
        allocation_fraction = Decimal(str(capital_rule["allocation_fraction"]))
        plan_identity = plan_identity_for(
            signal_identity=signal_identity,
            actual_quantity=portfolio.position_quantity,
            target_quantity=target_quantity,
            cycle_target_quantity=cycle_target,
            plan_mode=plan_mode,
            capital_mode=capital_mode,
            allocation_fraction=allocation_fraction,
            orders=orders,
            legs=legs,
        )
        return ExecutionPlan(
            strategy=self._identity,
            signal_identity=signal_identity,
            plan_identity=plan_identity,
            symbol=self._identity.symbol,
            signal_date=signal_date,
            trading_date=point.trading_date,
            generated_at=point.calculation_time,
            expected_portfolio_revision=portfolio.revision,
            expected_state_revision=state.revision,
            actual_quantity=portfolio.position_quantity,
            target_quantity=target_quantity,
            cycle_target_quantity=cycle_target,
            target_position=signal.target_position,
            action=str(raw["action"]),
            plan_mode=plan_mode,
            capital_mode=capital_mode,
            allocation_fraction=allocation_fraction,
            orders=orders,
            legs=legs,
            available_cash=portfolio.available_cash,
            fee_rate=Decimal(str(raw["fee_rate"])),
            estimated_order_cost=Decimal(str(raw["estimated_order_cost"])),
            unallocated_cash=Decimal(str(raw["unallocated_cash"])),
            references=references,
            required_capabilities=ExecutionCapabilities(
                required_orders,
                required_checkpoints,
            ),
            input_identities=data.input_identities,
            price_identities=data.price_identities,
            evidence=MappingProxyType(dict(signal.evidence)),
        )

    def run_window(self, *, executor: WindowExecutor):
        data = self._prepared()
        for trading_date in data.trading_dates():
            signal_date = data.signal_date_for(trading_date)
            point = TradingPoint(
                trading_date,
                datetime.combine(signal_date, time(20, 31), tzinfo=_SHANGHAI),
            )
            portfolio, state = executor.snapshot(point)
            plan = self.plan_at(
                point=point,
                portfolio=portfolio,
                state=state,
            )
            missing_orders = set(plan.required_capabilities.order_types) - set(
                executor.capabilities.order_types
            )
            missing_checkpoints = set(plan.required_capabilities.checkpoints) - set(
                executor.capabilities.checkpoints
            )
            if missing_orders or missing_checkpoints:
                raise RuntimeCompatibilityError(
                    "window executor lacks required capabilities: "
                    f"order_types={sorted(value.value for value in missing_orders)}, "
                    f"checkpoints={sorted(missing_checkpoints)}"
                )
            outcome = executor.execute(plan)
            if outcome.plan_identity != plan.plan_identity:
                raise RuntimeContractError("executor outcome refers to another plan")
        return executor.finish()
