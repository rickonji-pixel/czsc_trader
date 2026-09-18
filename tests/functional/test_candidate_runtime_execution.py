from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, time
import importlib
from pathlib import Path
import shutil
import sys
from zoneinfo import ZoneInfo

import pandas as pd
from pandas.testing import assert_frame_equal
import pytest

from strategy_runtime import (
    DeploymentSpec,
    RuntimeCompatibilityError,
    RuntimeContractError,
    StrategyCandidate,
    StrategyDecision,
    StrategyLoader,
    StrategyRelease,
    StrategyRunner,
    canonical_sha256,
)
from strategy_runtime import implementation_identity
import strategy_runtime.strategies
from trading_execution_engine import HistoricalExecutor


@pytest.fixture
def candidate_payload(tmp_path, monkeypatch):
    package = tmp_path / "runtime"
    strategies = package / "strategies"
    strategies.mkdir(parents=True)
    source = Path(__file__).parents[1] / "fixtures" / "candidate_runtime.py"
    shutil.copyfile(source, strategies / "candidate_fixture.py")
    module_name = "strategy_runtime.strategies.candidate_fixture"
    monkeypatch.setattr(strategy_runtime.strategies, "__path__", [str(strategies)])
    monkeypatch.setattr(implementation_identity, "files", lambda _: package)
    importlib.invalidate_caches()
    payload = {
        "runtime": {
            "module": module_name,
            "qualname": "CandidateFixture",
            "contract_version": 1,
            "source_files": ["strategies/candidate_fixture.py"],
            "source_sha256": implementation_identity.implementation_sha256(
                ("strategies/candidate_fixture.py",),
            ),
        },
        "parameters": {"threshold": 0.5},
    }
    yield payload, package
    sys.modules.pop(module_name, None)


def _execute(strategy):
    sessions = pd.bdate_range("2026-09-14", periods=5)
    inputs = {"flow": pd.DataFrame({"Date": sessions, "Flow": [0.1, 0.8, 0.2, 0.9, 0.0]})}
    history = strategy.calculate_history(inputs, sessions)
    daily = pd.DataFrame({"dt": sessions, "open": 1.0, "close": 1.0})
    channel = HistoricalExecutor(
        strategy_reference=strategy.definition.release_id,
        execution_daily=daily,
        signal_daily=daily,
        execution_intraday=pd.DataFrame(columns=["dt", "open", "high", "low", "close"]),
        evaluation_start=sessions[1],
        evaluation_end=sessions[-1],
        initial_cash=100_000,
        execution_policy=strategy.definition.execution,
        order_types=strategy.definition.capabilities.order_types,
    )
    definition = strategy.definition
    for i in range(1, len(sessions)):
        timestamp = datetime.combine(
            sessions[i - 1].date(), time(20, 30), ZoneInfo("Asia/Shanghai")
        )
        account = channel.account_snapshot("fixture", timestamp)
        deployment = DeploymentSpec(
            "fixture",
            definition.release_id,
            definition.release_hash,
            "588080.SH",
            "fixture",
            channel.channel_id,
            channel.deployment_settings,
        )
        decision = StrategyDecision(
            f"DEC-{i}",
            "fixture",
            definition.release_id,
            definition.release_hash,
            definition.runtime_sha256,
            timestamp,
            datetime.combine(sessions[i].date(), time(9, 30), ZoneInfo("Asia/Shanghai")),
            float(history.iloc[i - 1]["target_position"]),
            account.revision,
            0,
            {"fixture": "d" * 64},
            {"signal_date": sessions[i - 1].date().isoformat()},
            {},
        )
        StrategyRunner().submit_precomputed(
            strategy=strategy,
            deployment=deployment,
            account_snapshot=account,
            channel=channel,
            decision=decision,
        )
    return history, channel.finalize()


def test_parameter_search_and_release_use_one_implementation_and_isolated_txe(candidate_payload):
    payload, _ = candidate_payload
    candidate = StrategyCandidate("S900", "C001", payload)
    loader = StrategyLoader()
    first = loader.load_candidate(candidate)
    changed = deepcopy(payload)
    changed["parameters"]["threshold"] = 1.0
    second = loader.load_candidate(StrategyCandidate("S900", "C001", changed))
    assert first.definition.version is None
    assert first.definition.identity_kind == "CANDIDATE"
    assert first.definition.runtime_sha256 != second.definition.runtime_sha256
    history, ledger = _execute(first)
    _, other = _execute(second)
    assert len(ledger.fills) == 3
    assert other.fills.empty
    _, repeat = _execute(first)
    assert_frame_equal(ledger.account_daily, repeat.account_daily, check_exact=True)

    # A real frozen identity binds the same source and parameters without a v1 Python wrapper.
    raw = {
        "schema_version": 3,
        "strategy_id": "S900",
        "version": "v1",
        "release_id": "S900-v1",
        "strategy_payload": payload,
    }
    raw["release_hash"] = canonical_sha256(raw)
    frozen = loader.load(StrategyRelease.from_mapping(raw))
    frozen_history, frozen_ledger = _execute(frozen)
    assert frozen.definition.identity_kind == "RELEASE"
    assert frozen.definition.implementation == first.definition.implementation
    assert frozen.definition.parameters == first.definition.parameters
    assert_frame_equal(history, frozen_history, check_exact=True)
    assert_frame_equal(ledger.account_daily, frozen_ledger.account_daily, check_exact=True)
    for table in ("orders", "fills", "trades"):
        expected, actual = getattr(ledger, table), getattr(frozen_ledger, table)
        economics = [name for name in expected.columns if not name.endswith("_id")]
        assert_frame_equal(expected[economics], actual[economics], check_exact=True)
    with pytest.raises(RuntimeCompatibilityError, match="validated StrategyRelease"):
        loader.load(candidate)
    with pytest.raises(RuntimeContractError, match="no frozen version"):
        replace(first.definition, version="v1")
    with pytest.raises(TypeError):
        candidate.payload["parameters"]["threshold"] = 99


def test_candidate_load_fails_closed_on_source_and_parameter_identity_errors(
    candidate_payload, monkeypatch
):
    payload, package = candidate_payload
    loader = StrategyLoader()
    original = StrategyCandidate("S900", "C001", payload)
    valid = loader.load_candidate(original)
    daily = pd.DataFrame({"dt": pd.to_datetime(["2026-09-17"]), "open": [1.0], "close": [1.0]})
    execution = dict(
        strategy_reference=valid.definition.release_id,
        execution_daily=daily,
        signal_daily=daily,
        execution_intraday=pd.DataFrame(columns=["dt", "high", "low"]),
        evaluation_start=pd.Timestamp("2026-09-17"),
        evaluation_end=pd.Timestamp("2026-09-17"),
        initial_cash=100_000,
        execution_policy=valid.definition.execution,
        order_types=valid.definition.capabilities.order_types,
    )
    for changes, reason in (
        ({"initial_cash": float("nan")}, "positive and finite"),
        ({"execution_daily": pd.concat([daily, daily])}, "unique"),
        ({"execution_daily": daily.assign(close=float("nan"))}, "positive and finite"),
        ({"evaluation_end": pd.Timestamp("2026-09-18")}, "do not cover"),
    ):
        with pytest.raises(RuntimeContractError, match=reason):
            HistoricalExecutor(**{**execution, **changes})
    bad_parameters = deepcopy(payload)
    bad_parameters["parameters"]["threshold"] = 0.9
    factory = type(valid)
    create = factory.from_candidate
    monkeypatch.setattr(factory, "from_candidate", lambda _: valid)
    with pytest.raises(RuntimeCompatibilityError, match="candidate identity"):
        loader.load_candidate(StrategyCandidate("S900", "C001", bad_parameters))
    monkeypatch.setattr(factory, "from_candidate", create)
    wrong_closure = deepcopy(payload)
    wrong_closure["runtime"]["source_files"] = ["../secrets.py"]
    with pytest.raises(RuntimeCompatibilityError, match="unsafe path"):
        loader.load_candidate(StrategyCandidate("S900", "C001", wrong_closure))
    missing = deepcopy(payload)
    missing["runtime"].pop("source_files")
    with pytest.raises(RuntimeCompatibilityError, match="incomplete"):
        loader.load_candidate(StrategyCandidate("S900", "C001", missing))
    path = package / "strategies/candidate_fixture.py"
    path.write_bytes(path.read_bytes() + b"\n# changed after submission\n")
    with pytest.raises(RuntimeCompatibilityError, match="source hash differs"):
        loader.load_candidate(original)
    changed = deepcopy(payload)
    changed["runtime"]["source_sha256"] = implementation_identity.implementation_sha256(
        ("strategies/candidate_fixture.py",),
    )
    with pytest.raises(RuntimeCompatibilityError, match="fresh process"):
        loader.load_candidate(StrategyCandidate("S900", "C001", changed))
