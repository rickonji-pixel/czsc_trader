"""Caller-neutral strategy instance and its planning operations."""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from types import MappingProxyType
from zoneinfo import ZoneInfo

import pandas as pd

from .contracts import (
    ExecutionCapabilities,
    ExecutionPlan,
    ExecutionState,
    OrderSide,
    OrderType,
    PlanLeg,
    PlannedOrder,
    PortfolioSnapshot,
    PriceReference,
    StrategyIdentity,
    TradingPoint,
    TradableWindow,
    WindowExecutor,
    plan_identity_for,
    signal_identity_for,
)
from .data import DataPreparationRequest, PreparedStrategyData, StrategyDataSource
from .errors import RuntimeCompatibilityError, RuntimeContractError
from .execution_planner import build_execution_plan
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
        window: TradableWindow,
        execution_policy,
    ) -> None:
        self._algorithm = algorithm
        self._identity = identity
        self._window = window
        self._execution_policy = execution_policy
        self._history_cache: tuple[str, pd.DataFrame, pd.DatetimeIndex] | None = None

    @property
    def identity(self) -> StrategyIdentity:
        return self._identity

    @property
    def window(self) -> TradableWindow:
        return self._window

    @property
    def definition(self):
        return self._algorithm.definition

    @property
    def execution_policy(self):
        return self._execution_policy

    def prepare_data(self, source: StrategyDataSource) -> PreparedStrategyData:
        prepared = source.prepare(
            DataPreparationRequest(self._identity, self.definition, self._window)
        )
        if prepared.strategy != self._identity or prepared.window != self._window:
            raise RuntimeContractError("data source returned data for another strategy instance")
        return prepared

    def _history(
        self, data: PreparedStrategyData
    ) -> tuple[pd.DataFrame, pd.DatetimeIndex]:
        frames = {
            name: result.dataframe
            for name, result in data._publication.input_results.items()
        }
        sessions = pd.DatetimeIndex(
            pd.to_datetime(data._tradable_dates, errors="raise"), name="dt"
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
                previous = history["target_position"].shift(1, fill_value=0.0).loc[
                    signal_session
                ]
                evidence["action"] = (
                    "BUY"
                    if target_position > float(previous)
                    else "SELL"
                    if target_position < float(previous)
                    else "HOLD"
                )
        return StrategySignal(
            valid_session=point.trading_date,
            target_position=target_position,
            evidence=evidence,
            next_state={
                "signal_date": signal_date.isoformat(),
                "target_position": target_position,
            },
        )

    def inspect_signals(self, data: PreparedStrategyData) -> pd.DataFrame:
        """Return a defensive copy of strategy signal history for diagnostics."""

        if data.strategy != self._identity or data.window != self._window:
            raise RuntimeContractError("prepared data belongs to another strategy instance")
        history, _ = self._history(data)
        return history.copy()

    def plan_at(
        self,
        *,
        data: PreparedStrategyData,
        point: TradingPoint,
        portfolio: PortfolioSnapshot,
        state: ExecutionState,
    ) -> ExecutionPlan:
        if data.strategy != self._identity or data.window != self._window:
            raise RuntimeContractError("prepared data belongs to another strategy instance")
        if not self._window.contains(point.trading_date):
            raise RuntimeContractError("trading point is outside the strategy window")
        if portfolio.symbol != self._identity.symbol:
            raise RuntimeContractError("portfolio symbol differs from strategy")
        if portfolio.as_of > point.calculation_time or state.as_of > point.calculation_time:
            raise RuntimeContractError("planning state is newer than calculation time")

        signal = self._calculate_signal(data, point)
        signal_date = data.signal_date_for(point.trading_date)
        if signal.evidence.get("signal_date") != signal_date.isoformat():
            raise RuntimeContractError("strategy signal evidence refers to another date")
        references = data._pricing.references_for_dates(
            signal_date,
            point.trading_date,
            timezone=point.calculation_time.tzinfo,
        )
        raw = dict(
            build_execution_plan(
                deployment_settings={
                    "cycle_target_quantity": state.cycle_target_quantity
                },
                available_cash=float(portfolio.available_cash),
                position_quantity=portfolio.position_quantity,
                target_position=signal.target_position,
                policy=self._execution_policy,
                signal_reference_price=references.signal_reference_price,
                execution_reference_price=references.execution_reference_price,
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
                {order.order_type for order in orders}
                | {leg.order.order_type for leg in legs},
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
            valid_session=point.trading_date,
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
            references=PriceReference(
                Decimal(str(references.signal_reference_price)),
                Decimal(str(references.execution_reference_price)),
                references.signal_price_basis,
                references.execution_price_basis,
            ),
            required_capabilities=ExecutionCapabilities(
                required_orders,
                required_checkpoints,
            ),
            input_identities=data.input_identities,
            price_identities=data.price_identities,
            evidence=MappingProxyType(dict(signal.evidence)),
        )

    def run_window(self, *, data: PreparedStrategyData, executor: WindowExecutor):
        for trading_date in data.trading_dates():
            signal_date = data.signal_date_for(trading_date)
            point = TradingPoint(
                trading_date,
                datetime.combine(signal_date, time(20, 31), tzinfo=_SHANGHAI),
            )
            portfolio, state = executor.snapshot(point)
            plan = self.plan_at(
                data=data,
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
