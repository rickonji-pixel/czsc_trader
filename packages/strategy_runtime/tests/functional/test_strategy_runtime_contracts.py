from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from dataflows import DataError, DataIdentity, DataRequest, DataResult, DataStatus
from strategy_runtime import (
    ExecutionPolicy,
    PublicationStatus,
    PublishedStrategyData,
    RuntimeContractError,
)
from strategy_runtime.models import (
    AccountSnapshot,
    CalculationRequest,
    CutoffRule,
    DecisionContract,
    DeploymentSpec,
    ImplementationRef,
    InputContract,
    InputRequirement,
    MonitoringPolicy,
    ParameterSet,
    RequiredCapabilities,
    RuntimeDefinition,
    StrategyStateSnapshot,
)


NOW = datetime.fromisoformat("2026-09-17T20:30:00+08:00")
RELEASE_HASH = "a" * 64


def _requirement() -> InputRequirement:
    return InputRequirement(
        name="daily_bars",
        dataset="etf.ohlcv",
        subject="588080.SH",
        frequency="daily",
        lookback_sessions=252,
        cutoff_rule=CutoffRule.SIGNAL_SESSION,
    )


def _definition() -> RuntimeDefinition:
    return RuntimeDefinition(
        schema_version=1,
        strategy_family_id="S007",
        version="v1",
        release_id="S007-v1",
        release_hash=RELEASE_HASH,
        implementation=ImplementationRef(
            "strategy_runtime.strategies.s007_v1",
            "S007V1",
            1,
            "b" * 64,
        ),
        parameters=ParameterSet({"entry_threshold": 0.1}),
        inputs=InputContract((_requirement(),)),
        decision=DecisionContract("TARGET_POSITION", 0.0, 1.0, "NEXT_SESSION"),
        execution=ExecutionPolicy("MARKETABLE_LIMIT", {"limit_ratio": 0.2}),
        monitoring=MonitoringPolicy("ROLLING", {"window_sessions": 60}),
        capabilities=RequiredCapabilities(("etf.ohlcv",), ("LIMIT",)),
    )


def _ready_data() -> DataResult:
    frame = pd.DataFrame({"Date": ["2026-09-17"], "Close": [1.0]})
    identity = DataIdentity(
        dataset="etf.ohlcv",
        source="test",
        symbol="588080.SH",
        data_start="2026-09-17T00:00:00",
        data_cutoff="2026-09-17T00:00:00",
        content_sha256="c" * 64,
    )
    return DataResult(DataStatus.READY, frame, identity)


def _data_request() -> DataRequest:
    return DataRequest(
        "etf.ohlcv",
        "588080.SH",
        "2026-09-17",
        "2026-09-17",
        "2026-09-17",
        "daily",
    )


def _deployment() -> DeploymentSpec:
    return DeploymentSpec(
        "dep-s007-paper",
        "S007-v1",
        RELEASE_HASH,
        "588080.SH",
        "s007-v1",
        "futu_simulate_cn",
    )


def test_runtime_definition_is_family_version_scoped_and_immutable() -> None:
    definition = _definition()

    assert definition.strategy_family_id == "S007"
    assert definition.release_id == "S007-v1"
    assert definition.parameters.sha256
    with pytest.raises(TypeError):
        definition.parameters.values["entry_threshold"] = 0.2

    nested = ParameterSet({"weights": {"price": 0.5}, "windows": [20, 60]})
    with pytest.raises(TypeError):
        nested.values["weights"]["price"] = 0.7
    assert nested.values["windows"] == (20, 60)


def test_runtime_definition_requires_input_capabilities() -> None:
    with pytest.raises(RuntimeContractError, match="input datasets"):
        RuntimeDefinition(
            schema_version=1,
            strategy_family_id="S007",
            version="v1",
            release_id="S007-v1",
            release_hash=RELEASE_HASH,
            implementation=ImplementationRef("runtime", "Strategy", 1, "b" * 64),
            parameters=ParameterSet({}),
            inputs=InputContract((_requirement(),)),
            decision=DecisionContract("TARGET_POSITION", 0.0, 1.0, "NEXT_SESSION"),
            execution=ExecutionPolicy("LIMIT", {}),
            monitoring=MonitoringPolicy("ROLLING", {}),
            capabilities=RequiredCapabilities(("macro.shibor_daily",), ("LIMIT",)),
        )


def test_ready_publication_requires_every_input_to_be_ready() -> None:
    incomplete = DataResult(
        DataStatus.INCOMPLETE,
        error=DataError("INCOMPLETE_DATA", "stale", retryable=True),
    )

    with pytest.raises(RuntimeContractError, match="every input"):
        PublishedStrategyData(
            "S007-v1",
            RELEASE_HASH,
            PublicationStatus.READY,
            "2026-09-17",
            {"daily_bars": _data_request()},
            {"daily_bars": incomplete},
        )


def test_calculation_request_requires_matching_explicit_snapshots() -> None:
    publication = PublishedStrategyData(
        "S007-v1",
        RELEASE_HASH,
        PublicationStatus.READY,
        "2026-09-17",
        {"daily_bars": _data_request()},
        {"daily_bars": _ready_data()},
    )
    account = AccountSnapshot("s007-v1", 100_000.0, 100_000.0, 0, 4, NOW)
    state = StrategyStateSnapshot("dep-s007-paper", RELEASE_HASH, 2, NOW, {"position": 0})

    request = CalculationRequest(_deployment(), publication, account, state, NOW)

    assert request.account.revision == 4
    assert request.state.revision == 2


def test_calculation_rejects_stale_or_foreign_state_identity() -> None:
    publication = PublishedStrategyData(
        "S007-v1",
        RELEASE_HASH,
        PublicationStatus.READY,
        "2026-09-17",
        {"daily_bars": _data_request()},
        {"daily_bars": _ready_data()},
    )
    account = AccountSnapshot("s007-v1", 100_000.0, 100_000.0, 0, 4, NOW)
    foreign = StrategyStateSnapshot("another-deployment", RELEASE_HASH, 2, NOW, {})

    with pytest.raises(RuntimeContractError, match="deployment IDs differ"):
        CalculationRequest(_deployment(), publication, account, foreign, NOW)
