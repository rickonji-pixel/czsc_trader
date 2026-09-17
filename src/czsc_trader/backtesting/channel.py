"""TDR-owned execution channel for deterministic historical backtests."""

from __future__ import annotations

import pandas as pd
from strategy_runtime import (
    ChannelCapabilities,
    ExecutionReceipt,
    ExecutionRequest,
    RuntimeContractError,
)

from .datasets import ReplayData
from .execution_replay import replay_account
from .intraday_overlay_replay import replay_intraday_overlay
from .result import BacktestResult
from .signal_replay import SignalReplay


class BacktestChannel:
    """Admit SRT requests and settle them through TDR's replay engine."""

    def __init__(
        self,
        *,
        signals: SignalReplay,
        replay_data: ReplayData,
        initial_cash: float,
        output_kind: str,
        order_types: tuple[str, ...],
        checkpoints: tuple[str, ...] = (),
        channel_id: str = "backtest",
    ) -> None:
        if not channel_id.strip():
            raise RuntimeContractError("backtest channel_id must be non-empty")
        if initial_cash <= 0:
            raise RuntimeContractError("backtest initial_cash must be positive")
        if output_kind not in {"TARGET_POSITION", "INTRADAY_OVERLAY"}:
            raise RuntimeContractError(
                f"unsupported backtest decision output kind: {output_kind}"
            )
        self._channel_id = channel_id.strip()
        self._capabilities = ChannelCapabilities(order_types, checkpoints)
        self._signals = signals
        self._replay_data = replay_data
        self._initial_cash = float(initial_cash)
        self._output_kind = output_kind
        self._requests: dict[str, ExecutionRequest] = {}
        self._receipts: dict[str, ExecutionReceipt] = {}
        self._result: BacktestResult | None = None

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
        if self._result is not None:
            raise RuntimeContractError("backtest channel is already finalized")
        key = idempotency_key.strip()
        if not key:
            raise RuntimeContractError("execution idempotency key must be non-empty")
        existing = self._requests.get(key)
        if existing is not None:
            if existing != request:
                raise RuntimeContractError("idempotency key was reused for another request")
            return self._receipts[key]

        receipt = ExecutionReceipt(
            key,
            True,
            request.decision.decision_id,
            "BUFFERED",
            "accepted for deterministic TDR settlement",
        )
        self._requests[key] = request
        self._receipts[key] = receipt
        return receipt

    def finalize(self) -> BacktestResult:
        """Settle the complete admitted sequence exactly once."""

        if self._result is not None:
            return self._result
        expected = self._signals.decisions.dropna(subset=["valid_session"])
        requests = list(self._requests.values())
        expected_ids = expected["decision_id"].astype(str).tolist()
        actual_ids = [request.decision.decision_id for request in requests]
        if actual_ids != expected_ids:
            raise RuntimeContractError(
                "backtest channel decision sequence differs from strategy replay"
            )
        for request, row in zip(requests, expected.itertuples(index=False), strict=True):
            decision = request.decision
            if float(decision.target_position) != float(row.target_position):
                raise RuntimeContractError(
                    f"backtest request target differs for decision {decision.decision_id}"
                )
            if pd.Timestamp(decision.valid_at).date() != pd.Timestamp(row.valid_session).date():
                raise RuntimeContractError(
                    f"backtest request valid session differs for decision {decision.decision_id}"
                )
        if self._output_kind == "INTRADAY_OVERLAY":
            self._result = replay_intraday_overlay(
                self._signals, self._replay_data, self._initial_cash
            )
        else:
            self._result = replay_account(
                self._signals, self._replay_data, self._initial_cash
            )
        return self._result
