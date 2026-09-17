"""Deterministic, idempotent execution channel for backtest hosts."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .errors import RuntimeContractError
from .models import ChannelCapabilities, ExecutionReceipt, ExecutionRequest


@runtime_checkable
class BacktestExecutionModel(Protocol):
    """Translate one validated strategy decision into a deterministic receipt."""

    def execute(self, request: ExecutionRequest, idempotency_key: str) -> ExecutionReceipt: ...


class BacktestChannel:
    """Backtest channel with strict idempotency and an injected fill model."""

    def __init__(
        self,
        execution_model: BacktestExecutionModel,
        *,
        order_types: tuple[str, ...],
        checkpoints: tuple[str, ...] = (),
        channel_id: str = "backtest",
    ) -> None:
        if not channel_id.strip():
            raise RuntimeContractError("backtest channel_id must be non-empty")
        self._channel_id = channel_id.strip()
        self._capabilities = ChannelCapabilities(order_types, checkpoints)
        self._execution_model = execution_model
        self._requests: dict[str, ExecutionRequest] = {}
        self._receipts: dict[str, ExecutionReceipt] = {}

    @property
    def channel_id(self) -> str:
        return self._channel_id

    @property
    def capabilities(self) -> ChannelCapabilities:
        return self._capabilities

    @property
    def receipts(self) -> tuple[ExecutionReceipt, ...]:
        return tuple(self._receipts.values())

    def submit(self, request: ExecutionRequest, idempotency_key: str) -> ExecutionReceipt:
        key = idempotency_key.strip()
        if not key:
            raise RuntimeContractError("execution idempotency key must be non-empty")
        existing = self._requests.get(key)
        if existing is not None:
            if existing != request:
                raise RuntimeContractError("idempotency key was reused for another request")
            return self._receipts[key]

        receipt = self._execution_model.execute(request, key)
        if receipt.idempotency_key != key:
            raise RuntimeContractError("backtest receipt idempotency key differs from request")
        self._requests[key] = request
        self._receipts[key] = receipt
        return receipt
