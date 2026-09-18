from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, time
import importlib
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
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


def test_tdr_candidate_replay_uses_srt_publication_and_txe_without_rule_parser(
    candidate_payload, tmp_path, monkeypatch,
):
    from dataflows import DataRequest, Dataflows
    from strategy_runtime import PublicationStatus, PublishedStrategyData, write_publication
    from czsc_trader.backtesting.datasets import ReplayData
    from czsc_trader.backtesting.models import StrategyIdentity, StrategySnapshot
    from czsc_trader.backtesting.srt_bridge import build_srt_signal_replay, replay_srt_account

    payload, _ = candidate_payload
    candidate = StrategyCandidate("S900", "C001", payload)
    strategy = StrategyLoader().load_candidate(candidate)
    definition = strategy.definition
    sessions = pd.bdate_range("2026-09-14", periods=5)
    inputs = pd.DataFrame({"Date": sessions, "Flow": [0.1, 0.8, 0.2, 0.9, 0.0]})
    request = DataRequest(
        "etf.share", "588080.SH", "2026-09-14", "2026-09-18", "2026-09-18", "daily",
    )
    result = Dataflows({"etf.share": lambda _: (inputs, {"vendor": "test"})}).fetch(request)
    publication = PublishedStrategyData(
        definition.release_id, definition.release_hash, PublicationStatus.READY,
        "2026-09-18", {"flow": request}, {"flow": result},
    )
    write_publication(publication, tmp_path)
    daily = pd.DataFrame({"dt": sessions, "open": 1.0, "close": 1.0})
    replay_data = ReplayData(
        "research", tmp_path,
        SimpleNamespace(daily=daily, symbol="588080.SH", asset_type="etf"),
        daily, pd.DataFrame(columns=["dt", "high", "low"]), "d" * 64, sessions[-1].date(),
    )
    snapshot = StrategySnapshot(
        StrategyIdentity("CANDIDATE", candidate.reference_id, "fixture"),
        candidate.runtime_identity_sha256, canonical_sha256(payload), payload, None,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("candidate replay must never call the old rule parser")

    monkeypatch.setattr("czsc_trader.baselines.resolve_strategy_payload", forbidden)
    loaded, signals = build_srt_signal_replay(
        snapshot=snapshot, replay_data=replay_data, start=sessions[1], end=sessions[-1],
        repository_root=tmp_path,
    )
    replay = replay_srt_account(
        strategy=loaded, signals=signals, replay_data=replay_data, initial_cash=100_000,
    )
    _, direct = _execute(strategy)
    assert_frame_equal(replay.account_daily, direct.account_daily, check_exact=True)
    assert len(replay.fills) == 3
    assert signals.support_data["runtime_sha256"] == definition.runtime_sha256
    changed = deepcopy(payload)
    changed["parameters"]["threshold"] = 0.9
    with pytest.raises(RuntimeContractError, match="release hashes"):
        build_srt_signal_replay(
            snapshot=replace(snapshot, strategy_payload=changed), replay_data=replay_data,
            start=sessions[1], end=sessions[-1], repository_root=tmp_path,
            publication=publication,
        )
    with pytest.raises(RuntimeContractError, match="release hashes"):
        build_srt_signal_replay(
            snapshot=snapshot, replay_data=replay_data, start=sessions[1], end=sessions[-1],
            repository_root=tmp_path, publication=replace(publication, release_hash="0" * 64),
        )


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
        ({"fee_rate_override": float("nan")}, "fee_rate_override"),
        ({"fee_rate_override": -0.01}, "fee_rate_override"),
        ({"fee_rate_override": 1.0}, "fee_rate_override"),
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


def test_candidate_evaluation_and_se_use_identical_txe_ledgers(candidate_payload, tmp_path, monkeypatch):
    from dataflows import DataRequest, Dataflows
    from strategy_runtime import PublicationStatus, PublishedStrategyData, write_publication
    from strategy_evaluator import (
        AuditStatus, ChampionAuditRequest, ReplayEvidence, audit_provisional_champion,
        hash_execution_evidence, hash_return_matrix, hash_audit_data,
    )
    from czsc_trader.backtesting.datasets import ReplayData
    from czsc_trader.candidate_evaluation import CandidateEvaluationContext, evaluate_candidate_payloads
    from czsc_trader.application.evaluation_evidence import build_champion_audit_request

    payload, _ = candidate_payload
    sessions = pd.bdate_range("2026-09-14", periods=6)
    daily = pd.DataFrame({"dt": sessions, "open": 1.0, "close": 1.0})
    # First buy cannot fill; next day the unchanged target must retry and fill.
    daily.loc[2, "open"] = 1.1
    inputs = pd.DataFrame({"Date": sessions, "Flow": [.1, .8, .8, .1, .0, .0]})
    request = DataRequest("etf.share", "588080.SH", "2026-09-14", "2026-09-21", "2026-09-21", "daily")
    fetched = Dataflows({"etf.share": lambda _: (inputs, {"vendor": "test"})}).fetch(request)
    payloads = []
    for candidate_id, threshold in (("C000", 1.0), ("C001", .5)):
        parameters = deepcopy(payload)
        parameters["parameters"]["threshold"] = threshold
        strategy = StrategyLoader().load_candidate(StrategyCandidate("S900", candidate_id, parameters))
        definition = strategy.definition
        write_publication(PublishedStrategyData(
            definition.release_id, definition.release_hash, PublicationStatus.READY,
            "2026-09-21", {"flow": request}, {"flow": fetched},
        ), tmp_path)
        payloads.append({"candidate_id": candidate_id, "strategy_id": "S900",
                         "strategy_payload": parameters, "is_incumbent": candidate_id == "C000"})
    replay_data = ReplayData(
        "research", tmp_path, SimpleNamespace(daily=daily, symbol="588080.SH", asset_type="etf"),
        daily, pd.DataFrame(columns=["dt", "high", "low"]), "d" * 64, sessions[-1].date(),
    )
    monkeypatch.setattr("czsc_trader.candidate_evaluation.load_replay_data", lambda *a, **kw: replay_data)
    def forbidden(*args, **kwargs):
        raise AssertionError("evaluation must not invoke the old strategy parser or simple backtest")
    monkeypatch.setattr("czsc_trader.baselines.resolve_strategy_payload", forbidden)
    monkeypatch.setattr("czsc_trader.research_backtest.run_period_backtests", forbidden)
    context = CandidateEvaluationContext(
        SimpleNamespace(root=tmp_path), "588080.SH", "etf",
        (("full", (sessions[1], sessions[-1])),), .001, 100_000, frequency_window_days=3,
        family_id="S900",
    )
    protocol = SimpleNamespace(development_cutoff="2026-09-21", incumbent_id="C000",
                               experiment_id="TEST", execution_policy_hash="e" * 64, standard_version="opc-v3")
    payloads = tuple(payloads)
    screening = evaluate_candidate_payloads(context, protocol, payloads, ("C001", "C000"), "SCREENING")
    formal = evaluate_candidate_payloads(context, protocol, payloads, ("C001", "C000"), "FORMAL")
    assert tuple(replace(row, measurement_tier="FORMAL") for row in screening) == formal
    assert formal[0].closed_trades == 1
    assert evaluate_candidate_payloads(replace(context, workers=2), protocol, payloads, ("C001", "C000"), "FORMAL") == formal
    stressed = evaluate_candidate_payloads(context, protocol, payloads, ("C001",), "STRESS", ("total_cost_20bp",))
    assert stressed[0].total_return < formal[0].total_return
    assert stressed[0].cost_drag > formal[0].cost_drag
    audit_request = build_champion_audit_request(
        run_context=context, protocol=protocol, manifest={}, payloads=payloads, candidates=(), trials=(),
        ranking=SimpleNamespace(champion_id="C001", profiles=()), screening_profiles=(),
        formal=formal, repeated=(formal[0],), stress=stressed, search_candidate_ids=("C001",),
    )
    assert isinstance(audit_request.execution, ReplayEvidence)
    evidence = audit_request.execution
    assert [row["status"] for row in evidence.orders] == ["UNFILLED", "FILLED", "FILLED"]
    assert len(evidence.fills) == 2
    assert len(evidence.trades) == 1
    assert audit_request.search_returns.returns == tuple((row[0],) for row in audit_request.comparison_returns.returns)
    assert (1 + pd.Series([row[0] for row in audit_request.search_returns.returns])).prod() - 1 == pytest.approx(formal[0].total_return)
    restored = ChampionAuditRequest.from_dict(audit_request.to_dict())
    assert restored.to_dict() == audit_request.to_dict()
    audit = audit_provisional_champion(restored)
    execution = next(item for item in audit.findings if item.audit_id == "execution")
    assert execution.status is AuditStatus.PASS, execution.reason_codes

    # Individually hash-valid matrices must still agree with the audited ledger.
    mismatched = replace(audit_request.comparison_returns,
                         returns=tuple((row[0] + .01, *row[1:]) for row in audit_request.comparison_returns.returns))
    mismatched = replace(mismatched, content_hash=hash_return_matrix(mismatched))
    bad_request = replace(audit_request, comparison_returns=mismatched,
                          identity=replace(audit_request.identity, data_hash=hash_audit_data(audit_request.search_returns, mismatched)))
    assert "CHAMPION_LEDGER_RETURN_MISMATCH" in audit_provisional_champion(bad_request).reason_codes

    # A ledger defect must fail the real replay audit even with a freshly computed hash.
    broken = replace(evidence, fills=({**evidence.fills[0], "fees": 999.0}, *evidence.fills[1:]))
    broken = replace(broken, content_hash=hash_execution_evidence(broken))
    failed = audit_provisional_champion(replace(audit_request, execution=broken))
    assert next(item for item in failed.findings if item.audit_id == "execution").status is AuditStatus.FAIL
    with pytest.raises(ValueError, match="price-slippage"):
        evaluate_candidate_payloads(context, protocol, payloads, ("C001",), "STRESS", ("slippage_15bp",))
    with pytest.raises(ValueError, match="invalid cost"):
        evaluate_candidate_payloads(context, protocol, payloads, ("C001",), "STRESS", ("fee_xnan",))
