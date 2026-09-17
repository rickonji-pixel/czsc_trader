from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from dataflows import DataError, DataIdentity, DataRequest, DataResult, DataStatus, Dataflows
from strategy_runtime import (
    AccountSnapshot,
    BacktestChannel,
    CalculationRequest,
    CutoffRule,
    DecisionContract,
    DeploymentSpec,
    ExecutionPolicy,
    ExecutionReceipt,
    ImplementationRef,
    InputContract,
    InputRequirement,
    MonitoringPolicy,
    ParameterSet,
    PublicationStatus,
    PublishedStrategyData,
    RequiredCapabilities,
    RuntimeCompatibilityError,
    RuntimeContractError,
    RuntimeDefinition,
    RuntimeRunStatus,
    StrategyDecision,
    StrategyRunner,
    StrategyStateSnapshot,
)


NOW = datetime.fromisoformat("2026-09-17T20:30:00+08:00")
RELEASE_HASH = "a" * 64
INPUT_HASH = "c" * 64


def _definition(*, order_types: tuple[str, ...] = ("LIMIT",)) -> RuntimeDefinition:
    return RuntimeDefinition(
        schema_version=1,
        strategy_family_id="S007",
        version="v1",
        release_id="S007-v1",
        release_hash=RELEASE_HASH,
        implementation=ImplementationRef("tests.runtime", "FakeStrategy", 1, "b" * 64),
        parameters=ParameterSet({"threshold": 0.1}),
        inputs=InputContract(
            (
                InputRequirement(
                    "daily_bars",
                    "etf.ohlcv",
                    "588080.SH",
                    "daily",
                    60,
                    CutoffRule.SIGNAL_SESSION,
                ),
            )
        ),
        decision=DecisionContract("TARGET_POSITION", 0.0, 1.0, "NEXT_SESSION"),
        execution=ExecutionPolicy("MARKETABLE_LIMIT", {"limit_ratio": 0.2}),
        monitoring=MonitoringPolicy("ROLLING", {"window_sessions": 60}),
        capabilities=RequiredCapabilities(("etf.ohlcv",), order_types),
    )


def _deployment() -> DeploymentSpec:
    return DeploymentSpec(
        "dep-s007-backtest",
        "S007-v1",
        RELEASE_HASH,
        "588080.SH",
        "s007-backtest",
        "backtest",
    )


def _ready_publication() -> PublishedStrategyData:
    data = DataResult(
        DataStatus.READY,
        pd.DataFrame({"Date": ["2026-09-17"], "Close": [1.0]}),
        DataIdentity(
            "etf.ohlcv",
            "test",
            "588080.SH",
            "2026-09-17T00:00:00",
            "2026-09-17T00:00:00",
            INPUT_HASH,
        ),
    )
    return PublishedStrategyData(
        "S007-v1",
        RELEASE_HASH,
        PublicationStatus.READY,
        "2026-09-17",
        {
            "daily_bars": DataRequest(
                "etf.ohlcv",
                "588080.SH",
                "2026-09-17",
                "2026-09-17",
                "2026-09-17",
                "daily",
            )
        },
        {"daily_bars": data},
    )


def _not_ready_publication() -> PublishedStrategyData:
    data = DataResult(
        DataStatus.INCOMPLETE,
        error=DataError("INCOMPLETE_DATA", "source cutoff is stale", retryable=True),
    )
    return PublishedStrategyData(
        "S007-v1",
        RELEASE_HASH,
        PublicationStatus.INCOMPLETE,
        "2026-09-17",
        {
            "daily_bars": DataRequest(
                "etf.ohlcv",
                "588080.SH",
                "2026-09-17",
                "2026-09-17",
                "2026-09-17",
                "daily",
            )
        },
        {"daily_bars": data},
        "daily_bars did not reach the required cutoff",
    )


class FakeAccount:
    def __init__(self) -> None:
        self.calls = 0

    def snapshot(self, deployment: DeploymentSpec) -> AccountSnapshot:
        self.calls += 1
        return AccountSnapshot(deployment.account_id, 100_000.0, 100_000.0, 0, 3, NOW)


class FakeStrategy:
    def __init__(
        self,
        publication: PublishedStrategyData,
        *,
        target_position: float = 1.0,
        definition: RuntimeDefinition | None = None,
    ) -> None:
        self._definition = definition or _definition()
        self.publication = publication
        self.target_position = target_position
        self.publish_calls = 0
        self.calculate_calls = 0

    @property
    def definition(self) -> RuntimeDefinition:
        return self._definition

    def publish_data(
        self, dataflows: Dataflows, deployment: DeploymentSpec, through: datetime
    ) -> PublishedStrategyData:
        self.publish_calls += 1
        return self.publication

    def calculate(self, request: CalculationRequest) -> StrategyDecision:
        self.calculate_calls += 1
        return StrategyDecision(
            "DEC-20260917-2030-TEST",
            request.deployment.deployment_id,
            request.deployment.release_id,
            request.deployment.release_hash,
            request.calculation_time,
            request.calculation_time + timedelta(days=1),
            self.target_position,
            request.account.revision,
            request.state.revision,
            {"daily_bars": INPUT_HASH},
            {"score": 0.8},
            {"last_target": self.target_position},
        )

    def explain(self, decision: StrategyDecision):
        raise NotImplementedError


class FakeExecutionModel:
    def __init__(self, *, accepted: bool = True) -> None:
        self.accepted = accepted
        self.calls = 0

    def execute(self, request, idempotency_key: str) -> ExecutionReceipt:
        self.calls += 1
        return ExecutionReceipt(
            idempotency_key,
            self.accepted,
            f"BT-{self.calls}",
            "FILLED" if self.accepted else "REJECTED",
            "deterministic test execution",
        )


def _state() -> StrategyStateSnapshot:
    return StrategyStateSnapshot("dep-s007-backtest", RELEASE_HASH, 2, NOW, {})


def _run(strategy: FakeStrategy, account: FakeAccount, channel: BacktestChannel):
    return StrategyRunner().run(
        strategy=strategy,
        deployment=_deployment(),
        state=_state(),
        dataflows=Dataflows(),
        account=account,
        channel=channel,
        through=NOW,
        calculation_time=NOW,
    )


def test_runner_completes_one_deterministic_backtest_cycle() -> None:
    model = FakeExecutionModel()
    channel = BacktestChannel(model, order_types=("LIMIT",))
    strategy = FakeStrategy(_ready_publication())
    account = FakeAccount()

    result = _run(strategy, account, channel)

    assert result.status is RuntimeRunStatus.ACCEPTED
    assert result.receipt is not None and result.receipt.status == "FILLED"
    assert strategy.publish_calls == strategy.calculate_calls == account.calls == model.calls == 1


def test_runner_stops_before_snapshot_when_data_is_not_ready() -> None:
    model = FakeExecutionModel()
    channel = BacktestChannel(model, order_types=("LIMIT",))
    strategy = FakeStrategy(_not_ready_publication())
    account = FakeAccount()

    result = _run(strategy, account, channel)

    assert result.status is RuntimeRunStatus.DATA_NOT_READY
    assert strategy.calculate_calls == account.calls == model.calls == 0


def test_runner_rejects_fake_ready_data_before_calculation() -> None:
    stale_request = DataRequest(
        "etf.ohlcv",
        "588080.SH",
        "2026-09-16",
        "2026-09-17",
        "2026-09-16",
        "daily",
    )
    stale_result = DataResult(
        DataStatus.READY,
        pd.DataFrame({"Date": ["2026-09-16"], "Close": [1.0]}),
        DataIdentity(
            "etf.ohlcv",
            "test",
            "588080.SH",
            "2026-09-16T00:00:00",
            "2026-09-16T00:00:00",
            INPUT_HASH,
        ),
    )
    fake_ready = PublishedStrategyData(
        "S007-v1",
        RELEASE_HASH,
        PublicationStatus.READY,
        "2026-09-17",
        {"daily_bars": stale_request},
        {"daily_bars": stale_result},
    )
    strategy = FakeStrategy(fake_ready)
    account = FakeAccount()
    model = FakeExecutionModel()

    with pytest.raises(RuntimeContractError, match="cutoff differs from signal session"):
        _run(strategy, account, BacktestChannel(model, order_types=("LIMIT",)))
    assert strategy.calculate_calls == account.calls == model.calls == 0


def test_runner_rejects_unsupported_channel_before_publication() -> None:
    model = FakeExecutionModel()
    channel = BacktestChannel(model, order_types=("MARKET",))
    strategy = FakeStrategy(_ready_publication())

    with pytest.raises(RuntimeCompatibilityError, match="lacks required capabilities"):
        _run(strategy, FakeAccount(), channel)
    assert strategy.publish_calls == model.calls == 0


def test_runner_rejects_decision_outside_declared_bounds() -> None:
    model = FakeExecutionModel()
    channel = BacktestChannel(model, order_types=("LIMIT",))
    strategy = FakeStrategy(_ready_publication(), target_position=1.5)

    with pytest.raises(RuntimeContractError, match="target_position"):
        _run(strategy, FakeAccount(), channel)
    assert model.calls == 0


def test_backtest_channel_replays_same_idempotent_result() -> None:
    model = FakeExecutionModel()
    channel = BacktestChannel(model, order_types=("LIMIT",))
    strategy = FakeStrategy(_ready_publication())
    account = FakeAccount()

    first = _run(strategy, account, channel)
    second = _run(strategy, account, channel)

    assert first.receipt == second.receipt
    assert len(channel.receipts) == 1
    assert model.calls == 1


def test_runner_preserves_channel_rejection_as_an_explicit_result() -> None:
    model = FakeExecutionModel(accepted=False)
    channel = BacktestChannel(model, order_types=("LIMIT",))

    result = _run(FakeStrategy(_ready_publication()), FakeAccount(), channel)

    assert result.status is RuntimeRunStatus.REJECTED
    assert result.receipt is not None and result.receipt.status == "REJECTED"


def test_backtest_channel_rejects_idempotency_key_collision() -> None:
    model = FakeExecutionModel()
    channel = BacktestChannel(model, order_types=("LIMIT",))
    _run(FakeStrategy(_ready_publication(), target_position=1.0), FakeAccount(), channel)

    with pytest.raises(RuntimeContractError, match="reused for another request"):
        _run(FakeStrategy(_ready_publication(), target_position=0.0), FakeAccount(), channel)
    assert model.calls == 1
