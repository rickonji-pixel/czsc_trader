from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from dataflows import DataError, DataIdentity, DataRequest, DataResult, DataStatus, Dataflows
from strategy_runtime import (
    AccountSnapshot,
    CalculationRequest,
    ChannelCapabilities,
    CutoffRule,
    DecisionContract,
    DeploymentSpec,
    ExecutionPricingData,
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
        execution=ExecutionPolicy(
            "FROZEN_RULE",
            {
                "capital": {
                    "allocation_fraction": 1.0,
                    "fee_rate": 0.001,
                    "mode": "full_available_cash",
                    "target_scope": "entry_cycle",
                },
                "entry": {"limit_parameter": 0.0, "order_type": "LIMIT"},
                "exit": {"limit_ratio": 0.1, "order_type": "LIMIT"},
                "instrument": {
                    "lot_size": 100,
                    "maximum_order_quantity": 1_000_000,
                    "price_limit_ratio": 0.1,
                    "price_tick": 0.001,
                },
            },
        ),
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
    dates = pd.bdate_range(end="2026-09-17", periods=60)
    data = DataResult(
        DataStatus.READY,
        pd.DataFrame({"Date": dates, "Close": [1.0] * len(dates)}),
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
                dates[0].date().isoformat(),
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
        valid_at: datetime | None = None,
    ) -> None:
        self._definition = definition or _definition()
        self.publication = publication
        self.target_position = target_position
        self.valid_at = valid_at
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
            self.definition.runtime_sha256,
            request.calculation_time,
            self.valid_at or request.calculation_time + timedelta(days=1),
            self.target_position,
            request.account.revision,
            request.state.revision,
            {"daily_bars": INPUT_HASH},
            {"score": 0.8, "signal_date": request.publication.requested_cutoff},
            {"last_target": self.target_position},
        )

    def explain(self, decision: StrategyDecision):
        raise NotImplementedError


class FakeExecutionModel:
    def __init__(self, *, accepted: bool = True) -> None:
        self.accepted = accepted
        self.calls = 0
        self.request = None

    def execute(self, request, idempotency_key: str) -> ExecutionReceipt:
        self.calls += 1
        self.request = request
        return ExecutionReceipt(
            idempotency_key,
            self.accepted,
            f"BT-{self.calls}",
            "FILLED" if self.accepted else "REJECTED",
            "deterministic test execution",
        )


class TestExecutionChannel:
    """Host-owned test double for the SRT ExecutionChannel protocol."""

    __test__ = False

    def __init__(self, model: FakeExecutionModel, *, order_types: tuple[str, ...]) -> None:
        self.channel_id = "backtest"
        self.capabilities = ChannelCapabilities(order_types)
        self.model = model
        self._requests = {}
        self._receipts = {}

    @property
    def receipts(self):
        return tuple(self._receipts.values())

    def submit(self, request, idempotency_key: str) -> ExecutionReceipt:
        existing = self._requests.get(idempotency_key)
        if existing is not None:
            if existing != request:
                raise RuntimeContractError("idempotency key was reused for another request")
            return self._receipts[idempotency_key]
        receipt = self.model.execute(request, idempotency_key)
        self._requests[idempotency_key] = request
        self._receipts[idempotency_key] = receipt
        return receipt


def _state() -> StrategyStateSnapshot:
    return StrategyStateSnapshot("dep-s007-backtest", RELEASE_HASH, 2, NOW, {})


def _run(strategy: FakeStrategy, account: FakeAccount, channel: TestExecutionChannel):
    dates = pd.bdate_range(end="2026-09-17", periods=60)
    frame = pd.DataFrame({"Date": dates, "Close": [1.0] * len(dates)})
    return StrategyRunner().run(
        strategy=strategy,
        deployment=_deployment(),
        state=_state(),
        dataflows=Dataflows(),
        execution_data=ExecutionPricingData("588080.SH", frame, frame),
        account=account,
        channel=channel,
        through=NOW,
        calculation_time=NOW,
    )


def test_runner_completes_one_deterministic_backtest_cycle() -> None:
    model = FakeExecutionModel()
    channel = TestExecutionChannel(model, order_types=("LIMIT",))
    strategy = FakeStrategy(_ready_publication())
    account = FakeAccount()

    result = _run(strategy, account, channel)

    assert result.status is RuntimeRunStatus.ACCEPTED
    assert result.receipt is not None and result.receipt.status == "FILLED"
    assert result.instruction is not None
    assert result.instruction.reference_prices.signal_at.isoformat() == (
        "2026-09-17T15:00:00+08:00"
    )
    assert result.instruction.reference_prices.valid_at.isoformat() == (
        "2026-09-18T20:30:00+08:00"
    )
    assert result.instruction.order_plan["action"] == "BUY"
    assert isinstance(result.instruction.order_plan_payload()["order"], dict)
    assert model.request is not None
    assert model.request.instruction == result.instruction
    assert strategy.publish_calls == strategy.calculate_calls == account.calls == model.calls == 1


def test_execution_pricing_rejects_different_adjusted_and_execution_sessions() -> None:
    adjusted = pd.DataFrame({"Date": ["2026-09-16"], "Close": [1.0]})
    execution = pd.DataFrame({"Date": ["2026-09-17"], "Close": [1.0]})

    with pytest.raises(RuntimeContractError, match="pricing sessions differ"):
        ExecutionPricingData("588080.SH", adjusted, execution)


def test_runner_stops_before_snapshot_when_data_is_not_ready() -> None:
    model = FakeExecutionModel()
    channel = TestExecutionChannel(model, order_types=("LIMIT",))
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
        _run(strategy, account, TestExecutionChannel(model, order_types=("LIMIT",)))
    assert strategy.calculate_calls == account.calls == model.calls == 0


def test_runner_rejects_unsupported_channel_before_publication() -> None:
    model = FakeExecutionModel()
    channel = TestExecutionChannel(model, order_types=("MARKET",))
    strategy = FakeStrategy(_ready_publication())

    with pytest.raises(RuntimeCompatibilityError, match="lacks required capabilities"):
        _run(strategy, FakeAccount(), channel)
    assert strategy.publish_calls == model.calls == 0


def test_runner_rejects_decision_outside_declared_bounds() -> None:
    model = FakeExecutionModel()
    channel = TestExecutionChannel(model, order_types=("LIMIT",))
    strategy = FakeStrategy(_ready_publication(), target_position=1.5)

    with pytest.raises(RuntimeContractError, match="target_position"):
        _run(strategy, FakeAccount(), channel)
    assert model.calls == 0


def test_runner_accepts_morning_catchup_after_decision_effective_time() -> None:
    calculation_time = datetime.fromisoformat("2026-09-18T10:00:00+08:00")
    strategy = FakeStrategy(
        _ready_publication(),
        valid_at=datetime.fromisoformat("2026-09-18T09:30:00+08:00"),
    )
    model = FakeExecutionModel()

    result = StrategyRunner().run(
        strategy=strategy,
        deployment=_deployment(),
        state=_state(),
        dataflows=Dataflows(),
        execution_data=ExecutionPricingData(
            "588080.SH",
            strategy.publication.input_results["daily_bars"].dataframe,
            strategy.publication.input_results["daily_bars"].dataframe,
        ),
        account=FakeAccount(),
        channel=TestExecutionChannel(model, order_types=("LIMIT",)),
        through=NOW,
        calculation_time=calculation_time,
    )

    assert result.status is RuntimeRunStatus.ACCEPTED
    assert result.decision is not None
    assert result.decision.valid_at < result.decision.generated_at


def test_runner_rejects_decision_effective_on_publication_session() -> None:
    strategy = FakeStrategy(
        _ready_publication(),
        valid_at=datetime.fromisoformat("2026-09-17T09:30:00+08:00"),
    )

    with pytest.raises(RuntimeContractError, match="must follow the publication cutoff"):
        _run(
            strategy,
            FakeAccount(),
            TestExecutionChannel(FakeExecutionModel(), order_types=("LIMIT",)),
        )


def test_host_channel_replays_same_idempotent_result() -> None:
    model = FakeExecutionModel()
    channel = TestExecutionChannel(model, order_types=("LIMIT",))
    strategy = FakeStrategy(_ready_publication())
    account = FakeAccount()

    first = _run(strategy, account, channel)
    second = _run(strategy, account, channel)

    assert first.receipt == second.receipt
    assert len(channel.receipts) == 1
    assert model.calls == 1


def test_runner_rejects_ready_publication_with_insufficient_history() -> None:
    publication = _ready_publication()
    result = publication.input_results["daily_bars"]
    short = DataResult(
        DataStatus.READY,
        result.dataframe.tail(1),
        result.identity,
    )
    invalid = PublishedStrategyData(
        publication.release_id,
        publication.release_hash,
        publication.status,
        publication.requested_cutoff,
        publication.input_requests,
        {"daily_bars": short},
    )

    with pytest.raises(RuntimeContractError, match="insufficient history"):
        StrategyRunner.validate_publication(FakeStrategy(invalid), invalid)


def test_runner_preserves_channel_rejection_as_an_explicit_result() -> None:
    model = FakeExecutionModel(accepted=False)
    channel = TestExecutionChannel(model, order_types=("LIMIT",))

    result = _run(FakeStrategy(_ready_publication()), FakeAccount(), channel)

    assert result.status is RuntimeRunStatus.REJECTED
    assert result.receipt is not None and result.receipt.status == "REJECTED"


def test_host_channel_rejects_idempotency_key_collision() -> None:
    model = FakeExecutionModel()
    channel = TestExecutionChannel(model, order_types=("LIMIT",))
    _run(FakeStrategy(_ready_publication(), target_position=1.0), FakeAccount(), channel)

    with pytest.raises(RuntimeContractError, match="reused for another request"):
        _run(FakeStrategy(_ready_publication(), target_position=0.0), FakeAccount(), channel)
    assert model.calls == 1
