"""One deterministic orchestration path for every SRT execution host."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
from dataflows import Dataflows

from .errors import RuntimeCompatibilityError, RuntimeContractError
from .models import (
    AccountSnapshot,
    CalculationRequest,
    CutoffRule,
    DeploymentSpec,
    ExecutionRequest,
    ExecutionReceipt,
    PublishedStrategyData,
    RuntimeRunResult,
    RuntimeRunStatus,
    RuntimeDefinition,
    StrategyDecision,
    StrategyStateSnapshot,
)
from .protocols import ExecutableStrategy, ExecutionChannel, RuntimeAccount


class StrategyRunner:
    """Publish, calculate, validate, and submit one strategy cycle."""

    @classmethod
    def validate_publication(
        cls, strategy: ExecutableStrategy, publication: PublishedStrategyData
    ) -> None:
        """Validate one publication against its strategy's declared input contract."""

        cls._validate_publication(strategy.definition, publication)

    def run(
        self,
        *,
        strategy: ExecutableStrategy,
        deployment: DeploymentSpec,
        state: StrategyStateSnapshot,
        dataflows: Dataflows,
        account: RuntimeAccount,
        channel: ExecutionChannel,
        through: datetime,
        calculation_time: datetime,
    ) -> RuntimeRunResult:
        if through.tzinfo is None:
            raise RuntimeContractError("publication through must be timezone-aware")
        definition = strategy.definition
        self._validate_compatibility(definition, deployment, channel)

        publication = strategy.publish_data(dataflows, deployment, through)
        self._validate_publication(definition, publication)
        if not publication.ready:
            return RuntimeRunResult(RuntimeRunStatus.DATA_NOT_READY, publication)

        account_snapshot = account.snapshot(deployment)
        request = CalculationRequest(
            deployment=deployment,
            publication=publication,
            account=account_snapshot,
            state=state,
            calculation_time=calculation_time,
        )
        decision = strategy.calculate(request)
        self._validate_decision(definition, request, decision)

        execution_request = ExecutionRequest(
            deployment=deployment,
            account=account_snapshot,
            decision=decision,
            policy=definition.execution,
        )
        idempotency_key = f"{deployment.channel_id}:{decision.decision_id}"
        receipt = channel.submit(execution_request, idempotency_key)
        if receipt.idempotency_key != idempotency_key:
            raise RuntimeContractError("execution receipt idempotency key differs from request")
        status = RuntimeRunStatus.ACCEPTED if receipt.accepted else RuntimeRunStatus.REJECTED
        return RuntimeRunResult(status, publication, decision, receipt)

    def submit_precomputed(
        self,
        *,
        strategy: ExecutableStrategy,
        deployment: DeploymentSpec,
        account_snapshot: AccountSnapshot,
        channel: ExecutionChannel,
        decision: StrategyDecision,
    ) -> ExecutionReceipt:
        """Validate and submit one batch-calculated historical decision.

        Historical replay calculates a complete decision series once for
        performance, then routes every row through the same execution boundary.
        """

        definition = strategy.definition
        self._validate_compatibility(definition, deployment, channel)
        if decision.deployment_id != deployment.deployment_id:
            raise RuntimeContractError("decision deployment ID differs from deployment")
        if decision.release_id != definition.release_id:
            raise RuntimeContractError("decision release ID differs from strategy")
        if decision.release_hash != definition.release_hash:
            raise RuntimeContractError("decision release hash differs from strategy")
        if decision.runtime_sha256 != definition.runtime_sha256:
            raise RuntimeContractError("decision runtime identity differs from strategy")
        if decision.account_revision != account_snapshot.revision:
            raise RuntimeContractError("decision account revision differs from snapshot")
        if not (
            definition.decision.minimum_target
            <= decision.target_position
            <= definition.decision.maximum_target
        ):
            raise RuntimeContractError("decision target_position exceeds declared bounds")
        execution_request = ExecutionRequest(
            deployment=deployment,
            account=account_snapshot,
            decision=decision,
            policy=definition.execution,
        )
        idempotency_key = f"{deployment.channel_id}:{decision.decision_id}"
        receipt = channel.submit(execution_request, idempotency_key)
        if receipt.idempotency_key != idempotency_key:
            raise RuntimeContractError("execution receipt idempotency key differs from request")
        return receipt

    def run_published(
        self,
        *,
        strategy: ExecutableStrategy,
        deployment: DeploymentSpec,
        state: StrategyStateSnapshot,
        publication: PublishedStrategyData,
        account: RuntimeAccount,
        channel: ExecutionChannel,
        calculation_time: datetime,
    ) -> RuntimeRunResult:
        """Calculate and submit from an already persisted READY publication."""

        definition = strategy.definition
        self._validate_compatibility(definition, deployment, channel)
        self._validate_publication(definition, publication)
        if not publication.ready:
            return RuntimeRunResult(RuntimeRunStatus.DATA_NOT_READY, publication)
        account_snapshot = account.snapshot(deployment)
        request = CalculationRequest(
            deployment=deployment,
            publication=publication,
            account=account_snapshot,
            state=state,
            calculation_time=calculation_time,
        )
        decision = strategy.calculate(request)
        self._validate_decision(definition, request, decision)
        execution_request = ExecutionRequest(
            deployment=deployment,
            account=account_snapshot,
            decision=decision,
            policy=definition.execution,
        )
        idempotency_key = f"{deployment.channel_id}:{decision.decision_id}"
        receipt = channel.submit(execution_request, idempotency_key)
        if receipt.idempotency_key != idempotency_key:
            raise RuntimeContractError("execution receipt idempotency key differs from request")
        status = RuntimeRunStatus.ACCEPTED if receipt.accepted else RuntimeRunStatus.REJECTED
        return RuntimeRunResult(status, publication, decision, receipt)

    @staticmethod
    def _validate_compatibility(
        definition: RuntimeDefinition,
        deployment: DeploymentSpec,
        channel: ExecutionChannel,
    ) -> None:
        if definition.release_id != deployment.release_id:
            raise RuntimeCompatibilityError("strategy and deployment release IDs differ")
        if definition.release_hash != deployment.release_hash:
            raise RuntimeCompatibilityError("strategy and deployment release hashes differ")
        if deployment.channel_id != channel.channel_id:
            raise RuntimeCompatibilityError("deployment and execution channel IDs differ")
        missing_orders = set(definition.capabilities.order_types) - set(
            channel.capabilities.order_types
        )
        missing_checkpoints = set(definition.capabilities.checkpoints) - set(
            channel.capabilities.checkpoints
        )
        if missing_orders or missing_checkpoints:
            raise RuntimeCompatibilityError(
                "execution channel lacks required capabilities: "
                f"order_types={sorted(missing_orders)}, "
                f"checkpoints={sorted(missing_checkpoints)}"
            )

    @staticmethod
    def _validate_publication(
        definition: RuntimeDefinition, publication: PublishedStrategyData
    ) -> None:
        if publication.release_id != definition.release_id:
            raise RuntimeContractError("strategy and publication release IDs differ")
        if publication.release_hash != definition.release_hash:
            raise RuntimeContractError("strategy and publication release hashes differ")
        expected_inputs = {item.name for item in definition.inputs.requirements}
        actual_inputs = set(publication.input_results)
        if actual_inputs != expected_inputs:
            raise RuntimeContractError(
                "publication inputs differ from the declared contract: "
                f"missing={sorted(expected_inputs - actual_inputs)}, "
                f"unexpected={sorted(actual_inputs - expected_inputs)}"
            )
        requested_cutoff = pd.Timestamp(publication.requested_cutoff)
        requirements = {item.name: item for item in definition.inputs.requirements}
        for name, requirement in requirements.items():
            data_request = publication.input_requests[name]
            data_result = publication.input_results[name]
            if str(data_request.dataset) != requirement.dataset:
                raise RuntimeContractError(f"publication dataset differs for input {name}")
            if data_request.symbol != requirement.subject:
                raise RuntimeContractError(f"publication subject differs for input {name}")
            if data_request.frequency != requirement.frequency:
                raise RuntimeContractError(f"publication frequency differs for input {name}")
            if requirement.cutoff_rule is CutoffRule.SIGNAL_SESSION:
                if (
                    data_request.required_cutoff is None
                    or pd.Timestamp(data_request.required_cutoff) != requested_cutoff
                ):
                    raise RuntimeContractError(
                        f"publication cutoff differs from signal session for input {name}"
                    )
            elif requirement.cutoff_rule is CutoffRule.PREVIOUS_SESSION:
                if (
                    data_request.required_cutoff is None
                    or pd.Timestamp(data_request.required_cutoff) >= requested_cutoff
                ):
                    raise RuntimeContractError(
                        f"publication cutoff is not before signal session for input {name}"
                    )
            if data_result.ready:
                identity = data_result.identity
                if identity.dataset != str(data_request.dataset):
                    raise RuntimeContractError(
                        f"published identity dataset differs for input {name}"
                    )
                if identity.symbol != data_request.symbol:
                    raise RuntimeContractError(
                        f"published identity subject differs for input {name}"
                    )
                if (
                    requirement.cutoff_rule is CutoffRule.LATEST_AVAILABLE
                    and requirement.maximum_staleness_days
                    and pd.Timestamp(identity.data_cutoff)
                    < requested_cutoff - pd.Timedelta(days=requirement.maximum_staleness_days)
                ):
                    raise RuntimeContractError(
                        f"published input exceeds maximum staleness for input {name}"
                    )
                StrategyRunner._validate_history_depth(
                    name,
                    requirement.lookback_sessions,
                    data_result.dataframe,
                )

    @staticmethod
    def _validate_history_depth(
        input_name: str,
        required_observations: int,
        dataframe: pd.DataFrame,
    ) -> None:
        """Reject READY inputs that do not contain the declared history depth.

        The contract name predates multi-frequency inputs.  Its operational
        meaning is the number of distinct observations at the declared
        frequency: bars for intraday/weekly data and sessions for daily data.
        Multi-row cross-sectional datasets are counted by their distinct
        observation date, never by raw row count.
        """

        if required_observations <= 0:
            return
        date_column = next(
            (
                candidate
                for candidate in (
                    "Date",
                    "date",
                    "datetime",
                    "dt",
                    "trade_date",
                    "cal_date",
                )
                if candidate in dataframe.columns
            ),
            None,
        )
        if date_column is None:
            raise RuntimeContractError(
                f"published input {input_name} has no observation timestamp"
            )
        observed = pd.to_datetime(dataframe[date_column], errors="coerce").dropna().nunique()
        if observed < required_observations:
            raise RuntimeContractError(
                f"published input {input_name} has insufficient history: "
                f"required={required_observations}, observed={observed}"
            )

    @staticmethod
    def _validate_decision(
        definition: RuntimeDefinition,
        request: CalculationRequest,
        decision: StrategyDecision,
    ) -> None:
        if decision.deployment_id != request.deployment.deployment_id:
            raise RuntimeContractError("decision deployment ID differs from request")
        if decision.release_id != definition.release_id:
            raise RuntimeContractError("decision release ID differs from strategy")
        if decision.release_hash != definition.release_hash:
            raise RuntimeContractError("decision release hash differs from strategy")
        if decision.runtime_sha256 != definition.runtime_sha256:
            raise RuntimeContractError("decision runtime identity differs from strategy")
        if decision.generated_at != request.calculation_time:
            raise RuntimeContractError("decision generated_at must equal calculation_time")
        publication_cutoff = pd.Timestamp(request.publication.requested_cutoff).date()
        if decision.valid_at.date() <= publication_cutoff:
            raise RuntimeContractError(
                "decision valid session must follow the publication cutoff"
            )
        if not (
            definition.decision.minimum_target
            <= decision.target_position
            <= definition.decision.maximum_target
        ):
            raise RuntimeContractError("decision target_position exceeds declared bounds")
        if decision.account_revision != request.account.revision:
            raise RuntimeContractError("decision account revision differs from request")
        if decision.state_revision != request.state.revision:
            raise RuntimeContractError("decision state revision differs from request")
        expected_identities = {
            name: result.identity.content_sha256
            for name, result in request.publication.input_results.items()
        }
        if dict(decision.input_identity_hashes) != expected_identities:
            raise RuntimeContractError("decision input identities differ from publication")
