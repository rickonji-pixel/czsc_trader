"""Caller-neutral strategy instance and its planning operations."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time
from decimal import Decimal
from types import MappingProxyType
from zoneinfo import ZoneInfo

from .contracts import (
    DecisionPoint,
    DecisionWindow,
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
    WindowExecutor,
    plan_identity_for,
    signal_identity_for,
)
from .data import DataPreparationRequest, PreparedStrategyData, StrategyDataSource
from .errors import RuntimeCompatibilityError, RuntimeContractError
from .execution_planner import build_execution_plan
from .models import (
    AccountSnapshot,
    CalculationRequest,
    DeploymentSpec,
    StrategyStateSnapshot,
)
from .validation import validate_decision


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
    """One immutable strategy identity, parameter set and decision window."""

    def __init__(self, *, algorithm, identity: StrategyIdentity, window: DecisionWindow) -> None:
        self._algorithm = algorithm
        self._identity = identity
        self._window = window

    @property
    def identity(self) -> StrategyIdentity:
        return self._identity

    @property
    def window(self) -> DecisionWindow:
        return self._window

    @property
    def definition(self):
        return self._algorithm.definition

    def prepare_data(self, source: StrategyDataSource) -> PreparedStrategyData:
        prepared = source.prepare(
            DataPreparationRequest(self._identity, self.definition, self._window)
        )
        if prepared.strategy != self._identity or prepared.window != self._window:
            raise RuntimeContractError("data source returned data for another strategy instance")
        return prepared

    def plan_at(
        self,
        *,
        data: PreparedStrategyData,
        point: DecisionPoint,
        portfolio: PortfolioSnapshot,
        state: ExecutionState,
    ) -> ExecutionPlan:
        if data.strategy != self._identity or data.window != self._window:
            raise RuntimeContractError("prepared data belongs to another strategy instance")
        if not self._window.contains(point.signal_date):
            raise RuntimeContractError("decision point is outside the strategy window")
        if portfolio.symbol != self._identity.symbol:
            raise RuntimeContractError("portfolio symbol differs from strategy")
        if portfolio.as_of > point.calculation_time or state.as_of > point.calculation_time:
            raise RuntimeContractError("planning state is newer than calculation time")

        publication = replace(
            data._publication,
            requested_cutoff=point.signal_date.isoformat(),
        )
        deployment = DeploymentSpec(
            deployment_id=f"plan:{portfolio.account_id}",
            release_id=self._identity.reference_id,
            release_hash=self._identity.release_hash,
            symbol=self._identity.symbol,
            account_id=portfolio.account_id,
            channel_id="strategy_runtime",
            settings={"cycle_target_quantity": state.cycle_target_quantity},
        )
        account = AccountSnapshot(
            portfolio.account_id,
            float(portfolio.available_cash),
            float(portfolio.total_assets),
            portfolio.position_quantity,
            portfolio.revision,
            portfolio.as_of,
        )
        legacy_state = StrategyStateSnapshot(
            deployment.deployment_id,
            self._identity.release_hash,
            state.revision,
            state.as_of,
            {},
        )
        decision = self._algorithm.calculate(
            CalculationRequest(
                deployment,
                publication,
                account,
                legacy_state,
                point.calculation_time,
            )
        )
        validate_decision(
            self.definition,
            expected_release_id=self._identity.reference_id,
            expected_release_hash=self._identity.release_hash,
            expected_runtime_sha256=self._identity.runtime_sha256,
            expected_account_revision=portfolio.revision,
            expected_state_revision=state.revision,
            expected_input_identities=dict(data.input_identities),
            decision=decision,
        )
        references = data._pricing.references_for(decision)
        raw = dict(
            build_execution_plan(
                deployment=deployment,
                account=account,
                decision=decision,
                policy=self.definition.execution,
                reference_prices=references,
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
            signal_date=point.signal_date,
            target_position=decision.target_position,
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
            signal_date=point.signal_date,
            valid_session=decision.valid_at.date(),
            generated_at=point.calculation_time,
            expected_portfolio_revision=portfolio.revision,
            expected_state_revision=state.revision,
            actual_quantity=portfolio.position_quantity,
            target_quantity=target_quantity,
            cycle_target_quantity=cycle_target,
            target_position=decision.target_position,
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
            evidence=MappingProxyType(dict(decision.evidence)),
        )

    def run_window(self, *, data: PreparedStrategyData, executor: WindowExecutor):
        for signal_date in data.decision_dates():
            point = DecisionPoint(
                signal_date,
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
