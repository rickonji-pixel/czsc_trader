"""Host and executable-strategy protocols for SRT."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from dataflows import Dataflows

from .models import (
    AccountSnapshot,
    CalculationRequest,
    ChannelCapabilities,
    DeploymentSpec,
    ExecutionReceipt,
    ExecutionRequest,
    PublishedStrategyData,
    RuntimeDefinition,
    StrategyDecision,
    StrategyExplanation,
)


@runtime_checkable
class RuntimeClock(Protocol):
    def now(self) -> datetime: ...


@runtime_checkable
class RuntimeAccount(Protocol):
    def snapshot(self, deployment: DeploymentSpec) -> AccountSnapshot: ...


@runtime_checkable
class ExecutionChannel(Protocol):
    @property
    def channel_id(self) -> str: ...

    @property
    def capabilities(self) -> ChannelCapabilities: ...

    def submit(self, request: ExecutionRequest, idempotency_key: str) -> ExecutionReceipt: ...


@runtime_checkable
class ExecutableStrategy(Protocol):
    @property
    def definition(self) -> RuntimeDefinition: ...

    def publish_data(
        self,
        dataflows: Dataflows,
        deployment: DeploymentSpec,
        through: datetime,
    ) -> PublishedStrategyData: ...

    def calculate(self, request: CalculationRequest) -> StrategyDecision: ...

    def explain(self, decision: StrategyDecision) -> StrategyExplanation: ...
