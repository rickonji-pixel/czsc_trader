"""SRT platform adapter from validated runtime facts to frozen execution rules."""

from __future__ import annotations

from typing import Any, Mapping

from .execution_rules import ExecutionRuleInput, build_frozen_execution_plan
from .models import (
    AccountSnapshot,
    DeploymentSpec,
    ExecutionPolicy,
    ReferencePriceSnapshot,
    StrategyDecision,
)


def build_execution_plan(
    *,
    deployment: DeploymentSpec,
    account: AccountSnapshot,
    decision: StrategyDecision,
    policy: ExecutionPolicy,
    reference_prices: ReferencePriceSnapshot,
) -> Mapping[str, Any]:
    """Build the concrete order plan carried by an SRT execution instruction."""

    return build_frozen_execution_plan(
        ExecutionRuleInput(
            policy_type=policy.policy_type,
            settings=policy.settings,
            deployment_settings=deployment.settings,
            available_cash=account.available_cash,
            position_quantity=account.position_quantity,
            target_position=decision.target_position,
            signal_reference_price=reference_prices.signal_reference_price,
            execution_reference_price=reference_prices.execution_reference_price,
        )
    )
