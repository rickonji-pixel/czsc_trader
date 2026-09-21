from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from czsc_trader.candidate_evaluation import (
    CandidateEvaluationContext,
    prepare_evaluation_workspace,
)
from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.evaluation_service import evaluate_experiment
from strategy_evaluator import (
    AuditIdentity,
    AuditStatus,
    ChampionAuditResult,
    MachineEvaluationReport,
    MachineVerdict,
    MetricObservation,
    MetricStatus,
    ReturnMatrixEvidence,
    RiskLabel,
    EvaluationProtocol,
)
from strategy_manager import canonical_sha256


def test_formal_evaluation_rejects_data_that_stops_before_development_cutoff(
    functional_repo: Path, monkeypatch
) -> None:
    protocol = EvaluationProtocol.from_dict(
        {
            "schema_version": 1,
            "standard_version": "opc-v3",
            "experiment_id": "STALE",
            "research_objective": "reject false success",
            "development_cutoff": "2026-09-02",
            "incumbent_id": "BuyHold",
            "incumbent_hash": "a" * 64,
            "decision_windows": ["full"],
            "target_windows": ["full"],
            "execution_policy_hash": "b" * 64,
            "tightened_margins": {},
            "shortlist_limit": 1,
            "target_requirements": [
                {
                    "metric": "full_return",
                    "direction": "maximize",
                    "minimum_improvement": 0.0,
                }
            ],
            "candidate_manifest": "candidate_manifest.json",
        }
    )
    stale = pd.DataFrame(
        {
            "dt": pd.to_datetime(["2026-09-01"]),
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "vol": [1.0],
            "amount": [1.0],
        }
    )
    def load_stale(**request):
        # Match the TDR execution-data boundary instead of accepting any type.
        assert request["end"] == date(2026, 9, 2)
        return SimpleNamespace(
            adjusted_daily=stale,
            execution_daily=stale,
            execution_intraday=pd.DataFrame(),
        )

    monkeypatch.setattr(
        "czsc_trader.candidate_evaluation.prepare_backtest_execution_data",
        load_stale,
    )
    context = CandidateEvaluationContext(
        RepositoryContext.discover(functional_repo, explicit_root=functional_repo),
        "588080.SH",
        "etf",
        (("full", (pd.Timestamp("2026-09-01"), pd.Timestamp("2026-09-02"))),),
    )

    with pytest.raises(ValueError, match="does not reach development cutoff"):
        prepare_evaluation_workspace(context, protocol)


def _write_evaluation_bundle(root: Path) -> Path:
    experiment = root / "experiments" / "S001" / "0904_TEST"
    experiment.mkdir(parents=True)
    protocol = {
        "schema_version": 1,
        "standard_version": "opc-v3",
        "experiment_id": "0904_TEST",
        "research_objective": "improve range",
        "development_cutoff": "2026-09-02",
        "incumbent_id": "S001-v2",
        "incumbent_hash": "a" * 64,
        "decision_windows": ["full"],
        "target_windows": ["full"],
        "execution_policy_hash": "b" * 64,
        "tightened_margins": {},
        "shortlist_limit": 12,
        "target_requirements": [
            {
                "metric": "full_return",
                "direction": "maximize",
                "minimum_improvement": 0.01,
            }
        ],
        "candidate_manifest": "candidate_manifest.json",
    }
    candidates = [
        {
            "candidate_id": "S001-v2",
            "strategy_hash": "a" * 64,
            "execution_policy_hash": "b" * 64,
            "behavior_hash": "incumbent-behavior",
            "is_incumbent": True,
            "strategy_payload": {"rule": {"enter": 1}},
        },
        {
            "candidate_id": "winner",
            "strategy_id": "S001",
            "strategy_name": "综合基线策略",
            "strategy_hash": "c" * 64,
            "execution_policy_hash": "b" * 64,
            "behavior_hash": "winner-behavior",
            "family": "range",
            "parameter_group": "weights",
            "parameter_distance": 0.1,
            "is_incumbent": False,
            "strategy_payload": {"rule": {"enter": 2}},
        },
        {
            "candidate_id": "z-duplicate",
            "strategy_id": "S001",
            "strategy_name": "综合基线策略",
            "strategy_hash": "d" * 64,
            "execution_policy_hash": "b" * 64,
            "behavior_hash": "winner-behavior",
            "family": "range",
            "parameter_group": "weights",
            "parameter_distance": 0.2,
            "is_incumbent": False,
            "strategy_payload": {"rule": {"enter": 3}},
        },
        {
            "candidate_id": "inferior",
            "strategy_id": "S001",
            "strategy_name": "综合基线策略",
            "strategy_hash": "e" * 64,
            "execution_policy_hash": "b" * 64,
            "behavior_hash": "inferior-behavior",
            "family": "range",
            "parameter_group": "weights",
            "parameter_distance": 0.3,
            "is_incumbent": False,
            "strategy_payload": {"rule": {"enter": 4}},
        },
    ]
    manifest = {
        "schema_version": 1,
        "symbol": "588080.SH",
        "asset_type": "etf",
        "fee_rate": 0.0005,
        "init_cash": 100000,
        "windows": {"full": {"start": "2021-01-04", "end": "2026-09-02"}},
        "candidates": candidates,
        "trials": [
            {
                "trial_id": f"trial-{row['candidate_id']}",
                "candidate_id": row["candidate_id"],
                "strategy_hash": row["strategy_hash"],
                "behavior_hash": row["behavior_hash"],
                "status": "COMPLETED",
            }
            for row in candidates
        ],
    }
    (experiment / "evaluation_protocol.json").write_text(
        json.dumps(protocol), encoding="utf-8"
    )
    (experiment / "candidate_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return experiment


def _fixed_runner(
    context,
    protocol,
    candidates,
    candidate_ids,
    tier,
    scenarios=("standard",),
):
    rows = []
    for candidate_id in candidate_ids:
        better = candidate_id == "winner"
        worse = candidate_id == "inferior"
        for scenario in scenarios:
            rows.append(
                MetricObservation(
                    candidate_id,
                    "full",
                    scenario,
                    tier,
                    0.11 if better else (0.01 if worse else 0.10),
                    0.55 if better else (0.05 if worse else 0.50),
                    -0.10 if not worse else -0.30,
                    1.1 if better else (0.03 if worse else 1.0),
                    MetricStatus.VALID,
                    2.1 if better else (0.5 if worse else 2.0),
                    MetricStatus.VALID,
                    20,
                    2.0,
                    0.01,
                    (("full_return", 0.55 if better else (0.05 if worse else 0.50)),),
                )
            )
    return tuple(rows)


def test_ft_t06_evaluation_audits_every_candidate_and_freezes_once(
    functional_repo: Path, monkeypatch
) -> None:
    experiment = _write_evaluation_bundle(functional_repo)
    context = RepositoryContext.discover(functional_repo, explicit_root=functional_repo)
    identity = AuditIdentity(
        "0904_TEST",
        "a" * 64,
        "2026-09-02",
        "b" * 64,
        "b" * 64,
        "opc-v3",
        "champion-audit-v1",
        7,
    )
    matrix = ReturnMatrixEvidence(
        ("2026-09-01", "2026-09-02"),
        ("winner", "S001-v2"),
        ((0.01, 0.0), (-0.01, 0.01)),
        "d" * 64,
    )
    sentinel = SimpleNamespace(search_returns=matrix, comparison_returns=matrix)
    audit = ChampionAuditResult(
        identity,
        "winner",
        AuditStatus.PASS,
        RiskLabel.MIXED,
        (),
        (),
        direction_flags=(("bootstrap", "MIXED"),),
    )
    report_payload = {
        "schema_version": 2,
        "report_id": "SE-0904_TEST-winner",
        "candidate_id": "winner",
        "candidate_hash": "c" * 64,
        "policy_id": "OPC-MACHINE-ELIGIBILITY",
        "policy_version": "v1",
        "evaluator_version": "machine-evaluation-v1",
        "risk_label": "MIXED",
        "checks": [],
        "machine_verdict": "ELIGIBLE_FOR_FREEZE_REVIEW",
        "reason_codes": [],
        "evidence_hash": "e" * 64,
        "audit_result": audit.to_dict(),
    }
    machine_report = MachineEvaluationReport(
        2,
        "SE-0904_TEST-winner",
        "winner",
        "c" * 64,
        "OPC-MACHINE-ELIGIBILITY",
        "v1",
        "machine-evaluation-v1",
        RiskLabel.MIXED,
        (),
        MachineVerdict.ELIGIBLE_FOR_FREEZE_REVIEW,
        (),
        "e" * 64,
        audit,
        canonical_sha256(report_payload),
    )
    monkeypatch.setattr(
        "czsc_trader.application.evaluation_service.build_champion_audit_request",
        lambda **_kwargs: sentinel,
    )
    monkeypatch.setattr(
        "czsc_trader.application.evaluation_service.evaluate_machine_eligibility",
        lambda request: machine_report
        if request.audit_request is sentinel
        else (_ for _ in ()).throw(AssertionError("unexpected audit request")),
    )

    result = evaluate_experiment(context, "0904_TEST", runner=_fixed_runner)
    repeated = evaluate_experiment(context, "0904_TEST", runner=_fixed_runner)
    artifacts = experiment / "artifacts"
    required = {
        "evaluation_result.json",
        "evaluation_report.md",
        "screening_metrics.csv",
        "screening_noninferiority.csv",
        "screening_decisions.csv",
        "formal_metrics.csv",
        "noninferiority.csv",
        "pareto_profiles.csv",
        "health_check.json",
        "trial_ledger.csv",
        "statistical_audit.json",
        "candidate_returns.csv",
        "comparison_returns.csv",
        "machine_evaluation.json",
    }
    present = {path.name for path in artifacts.iterdir()}
    assert required <= present, (result.result, sorted(present))
    assert result.result["decision"] == "RECOMMEND_FREEZE"
    assert repeated.result == result.result
    with (artifacts / "screening_decisions.csv").open(
        encoding="utf-8", newline=""
    ) as stream:
        decisions = {row["candidate_id"]: row for row in csv.DictReader(stream)}
    assert set(decisions) == {"winner", "z-duplicate", "inferior"}
    assert decisions["winner"]["outcome"] == "SHORTLISTED"
    assert decisions["z-duplicate"]["outcome"] == "BEHAVIOR_DEDUPLICATED"
    assert decisions["inferior"]["outcome"] == "SCREENING_NONINFERIORITY"
    stored_audit = json.loads(
        (artifacts / "statistical_audit.json").read_text(encoding="utf-8")
    )
    assert stored_audit["status"] == "PASS"
    assert stored_audit["risk_label"] == "MIXED"

    decisions_path = artifacts / "screening_decisions.csv"
    original_decisions = decisions_path.read_bytes()
    decisions_path.write_bytes(original_decisions + b"tampered\n")
    try:
        evaluate_experiment(context, "0904_TEST", runner=_fixed_runner)
    except ValueError as exc:
        assert "hash mismatch" in str(exc)
    else:
        raise AssertionError("tampered evaluation artifact was accepted")
    decisions_path.write_bytes(original_decisions)
