from __future__ import annotations

import json
from pathlib import Path

from strategy_manager import StrategyRegistry, canonical_sha256

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import strategy_source as strategy_source_module
from czsc_trader.backtesting.strategy_source import (
    resolve_candidate_snapshot,
    resolve_registered_strategy,
)

from functional_support import invoke_main, invoke_main_failure


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _evidence(
    evidence_id: str,
    phase: str,
    *,
    release_hash: str,
) -> dict:
    return {
        "schema_version": 1,
        "evidence_id": evidence_id,
        "strategy_id": "S900",
        "version": "v1",
        "release_hash": release_hash,
        "phase": phase,
        "period_start": "2021-01-01",
        "period_end": "2026-09-04",
        "data_identity": {"symbol": "588080.SH", "fixture": "functional"},
        "initial_capital": 100000.0,
        "fee_rate": 0.0005,
        "maximum_drawdown": -0.10,
        "calmar_ratio": 1.5,
        "win_loss_ratio": 1.3,
        "win_loss_ratio_status": "VALID",
        "total_return": 0.25,
        "sharpe_ratio": 0.8,
        "closed_trades": 12,
        "source_path": "functional://S900",
        "source_hash": "b" * 64,
        "recorded_at": "2026-09-04T19:00:00+08:00",
        "recorded_by": "tester",
    }


def test_backtest_strategy_sources_preserve_identity_and_rule(
    functional_repo: Path,
) -> None:
    context = RepositoryContext.discover(functional_repo)
    registered = resolve_registered_strategy(context, "S001", "v1")
    candidate_hash = "c" * 64
    candidate = resolve_candidate_snapshot(
        context,
        "0904_EX04:R1102",
        registered.strategy_payload,
        candidate_hash,
        "experiments/0904_EX04",
    )

    assert registered.identity.kind == "REGISTERED"
    assert registered.identity.reference == "S001-v1"
    assert candidate.identity.kind == "CANDIDATE"
    assert candidate.identity.reference == "0904_EX04:R1102"
    assert registered.source_hash == "ae422915ff736431d70e0381dd6514ee800d861060cc5568712b55c895ddfb62"
    assert candidate.source_hash == candidate_hash
    assert registered.content_hash
    assert candidate.content_hash
    assert registered.resolved_rule is None
    assert candidate.resolved_rule is not None
    assert registered.strategy_payload == candidate.strategy_payload


def test_registered_strategy_snapshot_does_not_require_tdr_rule_resolution(
    functional_repo: Path, monkeypatch
) -> None:
    context = RepositoryContext.discover(functional_repo)

    def fail_legacy_resolution(*args, **kwargs):
        raise AssertionError("registered SRT must not use the TDR legacy rule resolver")

    monkeypatch.setattr(
        strategy_source_module, "resolve_strategy_payload", fail_legacy_resolution
    )
    registered = resolve_registered_strategy(context, "S001", "v1")

    assert registered.identity.reference == "S001-v1"
    assert registered.resolved_rule is None


def test_ft_t05_strategy_cli_manages_a_complete_audited_lifecycle(
    functional_repo: Path, capsys, monkeypatch
) -> None:
    def runtime_pass(version):
        release_hash = version.release_hash or canonical_sha256(version.release_payload())
        return {
            "schema_version": 1,
            "status": "PASS",
            "release_id": version.release_id,
            "release_hash": release_hash,
            "runtime_sha256": "f" * 64,
        }

    monkeypatch.setattr(
        "czsc_trader.application.strategy_service.validate_runtime_readiness",
        runtime_pass,
    )
    strategy_input = _write_json(
        functional_repo / "strategy.json",
        {
            "schema_version": 1,
            "strategy_id": "S900",
            "name": "功能测试策略",
            "objective": "验证完整策略治理链路",
            "responsibility": "生成目标仓位",
            "scope": ["588080.SH"],
            "created_at": "2026-09-04T10:00:00+08:00",
            "created_by": "tester",
        },
    )
    version_input = _write_json(
        functional_repo / "version.json",
        {
            "schema_version": 1,
            "strategy_id": "S900",
            "version": "v1",
            "release_id": "S900-v1",
            "parent_version": None,
            "change_summary": "首个功能测试版本",
            "source_experiment": "experiments/0904_TEST",
            "source_candidate": "functional-winner",
            "selection_data_cutoff": "2026-09-02",
            "forward_start": "2026-09-05",
            "strategy_payload": {"symbol": "588080.SH", "target": "position"},
            "release_hash": None,
        },
    )
    research_path = _write_json(
        functional_repo / "research-evidence.json",
        _evidence(
            "EVD-S900-RESEARCH",
            "RESEARCH_BACKTEST",
            release_hash="a" * 64,
        ),
    )
    machine_report = {
        "schema_version": 2,
        "report_id": "SE-0904-TEST-functional-winner",
        "candidate_id": "functional-winner",
        "candidate_hash": "c" * 64,
        "machine_verdict": "ELIGIBLE_FOR_FREEZE_REVIEW",
        "risk_label": "MIXED",
    }
    machine_report["report_hash"] = canonical_sha256(machine_report)
    machine_artifact = functional_repo / "experiments" / "0904_TEST" / "artifacts"
    machine_artifact.mkdir(parents=True)
    _write_json(machine_artifact / "machine_evaluation.json", machine_report)
    health_path = _write_json(
        functional_repo / "freeze-health.json",
        {
            "machine_report": machine_report,
            "review": {
                "schema_version": 1,
                "strategy_id": "S900",
                "candidate_id": "functional-winner",
                "candidate_hash": "c" * 64,
                "decision": "APPROVE_FREEZE",
                "reviewed_by": "tester",
                "rationale": "functional lifecycle",
                "mechanism_review": "APPROVED",
                "external_relevance_review": "APPROVED",
                "deployment_review": "APPROVED",
                "monitoring_plan_review": "APPROVED",
                "reviewed_at": "2026-09-04T19:00:00+08:00",
            },
        },
    )
    root = ["--repo-root", str(functional_repo)]
    audit = ["--actor", "tester", "--reason", "functional lifecycle"]

    created = invoke_main(
        ["strategy", "create", "--input", str(strategy_input), *audit, *root],
        capsys,
    )
    listed_identity = invoke_main(["strategy", "list", *root], capsys)
    shown_identity = invoke_main(
        ["strategy", "show", "--strategy", "S900", *root], capsys
    )
    versioned = invoke_main(
        [
            "strategy",
            "version",
            "create",
            "--input",
            str(version_input),
            *audit,
            *root,
        ],
        capsys,
    )
    blocked_freeze = invoke_main_failure(
        [
            "strategy",
            "freeze",
            "--strategy",
            "S900",
            "--version",
            "v1",
            "--evidence",
            str(research_path),
            *audit,
            *root,
        ],
        capsys,
    )
    frozen = invoke_main(
        [
            "strategy",
            "freeze",
            "--strategy",
            "S900",
            "--version",
            "v1",
            "--evidence",
            str(research_path),
            "--health-check",
            str(health_path),
            *audit,
            *root,
        ],
        capsys,
    )
    release_hash = frozen["result"]["version"]["release_hash"]
    paper_path = _write_json(
        functional_repo / "paper-evidence.json",
        _evidence(
            "EVD-S900-PAPER",
            "PAPER_FORWARD",
            release_hash=release_hash,
        ),
    )
    invoke_main(
        ["strategy", "evidence", "add", "--input", str(paper_path), *root],
        capsys,
    )
    promoted = invoke_main(
        [
            "strategy",
            "promote",
            "--strategy",
            "S900",
            "--version",
            "v1",
            "--evidence",
            "EVD-S900-PAPER",
            *audit,
            *root,
        ],
        capsys,
    )
    downgraded = invoke_main(
        [
            "strategy",
            "downgrade",
            "--strategy",
            "S900",
            "--version",
            "v1",
            "--evidence",
            "EVD-S900-PAPER",
            *audit,
            *root,
        ],
        capsys,
    )
    retired = invoke_main(
        [
            "strategy",
            "retire",
            "--strategy",
            "S900",
            "--version",
            "v1",
            *audit,
            *root,
        ],
        capsys,
    )

    shown = invoke_main(
        ["strategy", "show", "--strategy", "S900", "--version", "v1", *root],
        capsys,
    )
    listed = invoke_main(["strategy", "list", *root], capsys)
    validated = invoke_main(["strategy", "validate", *root], capsys)
    history = invoke_main(
        ["strategy", "history", "--strategy", "S900", *root], capsys
    )
    performance = invoke_main(
        [
            "strategy",
            "performance",
            "--strategy",
            "S900",
            "--version",
            "v1",
            *root,
        ],
        capsys,
    )
    baseline_list = invoke_main(["baseline", "list", *root], capsys)
    baseline_show = invoke_main(
        [
            "baseline",
            "show",
            "--version",
            "baseline_20260903",
            "--symbol",
            "588080.SH",
            *root,
        ],
        capsys,
    )
    baseline_validate = invoke_main(
        [
            "baseline",
            "validate",
            "--version",
            "baseline_20260903",
            "--symbol",
            "588080.SH",
            *root,
        ],
        capsys,
    )

    assert created["result"]["strategy_id"] == "S900"
    pending = next(
        row
        for row in listed_identity["result"]["strategies"]
        if row["strategy_id"] == "S900"
    )
    assert pending["version"] is None
    assert pending["qualification"] is None
    assert shown_identity["result"]["objective"] == "验证完整策略治理链路"
    assert shown_identity["result"]["version"] is None
    assert versioned["result"]["version"]["release_id"] == "S900-v1"
    assert blocked_freeze["error"]["code"] == "invalid_arguments"
    assert len(release_hash) == 64
    assert promoted["result"]["to_state"] == "LIVE_READY"
    assert downgraded["result"]["to_state"] == "PAPER_READY"
    assert retired["result"]["to_state"] == "RETIRED"
    assert shown["result"]["qualification"] == "RETIRED"
    assert shown["result"]["release_hash"] == release_hash
    assert any(row["strategy_id"] == "S900" for row in listed["result"]["strategies"])
    assert validated["result"]["strategies"] == len(
        listed["result"]["strategies"]
    )
    assert [event["event_type"] for event in history["result"]["events"]] == [
        "VERSION_CREATED",
        "VERSION_FROZEN",
        "VERSION_PROMOTED",
        "VERSION_DOWNGRADED",
        "VERSION_RETIRED",
    ]
    assert len(performance["result"]["phases"]["RESEARCH_BACKTEST"]) == 1
    assert len(performance["result"]["phases"]["PAPER_FORWARD"]) == 1
    assert performance["result"]["phases"]["LIVE"] == []
    active_release = StrategyRegistry(functional_repo / "strategies").resolve_strategy(
        "baseline_20260903"
    )
    active_baseline = next(
        row
        for row in baseline_list["result"]["baselines"]
        if row["version"] == "baseline_20260903"
    )
    assert active_baseline["strategy_version"] == active_release.version
    assert baseline_show["result"]["strategy_version"] == active_release.version
    assert baseline_validate["result"]["strategy_version"] == active_release.version
