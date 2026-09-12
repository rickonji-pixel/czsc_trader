from __future__ import annotations

import json
from pathlib import Path

from strategy_evaluator import (
    AuditStatus,
    BenchmarkChallengeDecision,
    CandidateReadinessRequest,
    FreezeHealthRequest,
    RiskLabel,
    assess_freeze_health,
    assess_research_candidate,
)

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256, raw_file_sha256


EXPERIMENT_ID = "20260912_S004_EX17"
PTE_BLOCKER = "PTE_S004_OPEN_EXECUTION_NOT_IMPLEMENTED_OR_VALIDATED"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    sources = {
        "candidate": repo / "research/S004/candidates/S004-C001.json",
        "mechanism": repo / "experiments/S004/20260912_S004_EX06/artifacts/attribution_summary.json",
        "parameter_platform": repo / "experiments/S004/20260912_S004_EX07/artifacts/platform_summary.json",
        "construction": repo / "experiments/S004/20260912_S004_EX12/artifacts/candidate_metrics.json",
        "formal_replay": repo / "experiments/S004/20260912_S004_EX14/artifacts/equivalence_summary.json",
        "statistical": repo / "experiments/S004/20260912_S004_EX15/artifacts/statistical_summary.json",
        "benchmark": repo / "experiments/S004/20260912_S004_EX16/artifacts/benchmark_assessment.json",
    }
    missing = [name for name, path in sources.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"freeze health evidence is incomplete: {missing}")
    for source_id in (
        "20260912_S004_EX06",
        "20260912_S004_EX07",
        "20260912_S004_EX12",
        "20260912_S004_EX14",
        "20260912_S004_EX15",
        "20260912_S004_EX16",
    ):
        validate_experiment_archive(repo / "experiments" / "S004" / source_id)
    candidate = _read(sources["candidate"])
    construction = _read(sources["construction"])
    formal = _read(sources["formal_replay"])
    statistical = _read(sources["statistical"])
    benchmark = _read(sources["benchmark"])
    if candidate.get("candidate_spec_normalized_sha256") != protocol["candidate_hash"]:
        raise ValueError("candidate identity differs from health protocol")

    readiness_request = CandidateReadinessRequest(
        candidate_id=str(protocol["candidate_id"]),
        candidate_hash=str(protocol["candidate_hash"]),
        integrity=AuditStatus.PASS,
        reproducibility=AuditStatus.PASS,
        mechanism_evidence=RiskLabel.FAVORABLE,
        statistical_evidence=RiskLabel.WEAK,
        external_validation=RiskLabel.MIXED,
        primary_closed_trades=int(construction["evaluated_episodes"]),
        minimum_closed_trades=100,
    )
    readiness = assess_research_candidate(readiness_request)
    benchmark_decision = BenchmarkChallengeDecision(str(benchmark["result"]["decision"]))
    health_request = FreezeHealthRequest(
        candidate_id=str(protocol["candidate_id"]),
        candidate_hash=str(protocol["candidate_hash"]),
        candidate_readiness=(
            AuditStatus.PASS
            if readiness.decision.value == "RECOMMEND_REGISTRATION"
            else AuditStatus.FAIL
        ),
        benchmark_challenge=benchmark_decision,
        evidence_integrity=AuditStatus.PASS,
        reproducibility=AuditStatus.PASS,
        technical_replay=(AuditStatus.PASS if formal["se_replay_audit"] == "PASS" else AuditStatus.FAIL),
        cost_stress=AuditStatus.PASS,
        monitoring_plan=AuditStatus.INSUFFICIENT,
        mechanism_evidence=RiskLabel.FAVORABLE,
        statistical_evidence=RiskLabel.WEAK,
        parameter_robustness=RiskLabel.MIXED,
        external_validation=RiskLabel.MIXED,
        execution_evidence=RiskLabel.FAVORABLE,
        blocking_findings=(PTE_BLOCKER,),
    )
    health = assess_freeze_health(health_request)
    evidence_index = {
        name: {
            "path": str(path.relative_to(repo)).replace("\\", "/"),
            "sha256": raw_file_sha256(path),
        }
        for name, path in sources.items()
    }
    source = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "candidate_id": protocol["candidate_id"],
        "candidate_hash": protocol["candidate_hash"],
        "readiness_request": readiness_request.to_dict(),
        "readiness_result": readiness.to_dict(),
        "health_request": health_request.to_dict(),
        "health_result": health.to_dict(),
        "evidence_index": evidence_index,
        "risk_disclosures": [
            f"36条历史路径的PBO为{float(statistical['pbo']):.2%}",
            f"有效DSR概率为{float(statistical['dsr_effective_probability']):.2%}",
            "21日区块Bootstrap的年化收益90%下界低于零",
            "原始尾盘跌幅参数平台仅4/27个网格完整通过",
            "尚无跨标的复现证据",
            "收益主要发生在次日开盘后至10:00前",
        ],
        "blocking_findings": [PTE_BLOCKER],
        "lifecycle_effects": {
            "candidate_deleted": False,
            "strategy_version_created": False,
            "strategy_frozen": False,
            "pte_deployed": False,
        },
    }
    assessment = {
        "source": source,
        "approval": {
            "schema_version": 1,
            "assessment_id": "FHC-20260912-S004-C001",
            "strategy_id": "S004",
            "candidate_id": protocol["candidate_id"],
            "candidate_hash": protocol["candidate_hash"],
            "decision": health.decision.value,
            "risk_label": health.risk_label.value,
            "source_experiment": f"experiments/S004/{EXPERIMENT_ID}",
            "source_hash": canonical_json_sha256(source),
            "assessed_at": "2026-09-12T23:00:00+08:00",
        },
    }
    _write(artifacts / "freeze_health.json", assessment)
    (experiment / "03_execution.md").write_text(
        "# 20260912_S004_EX17 执行\n\n状态：COMPLETE。SE已汇总候选身份、机制归因、36条路径搜索惩罚、正式回放、成本压力、BuyHold PK、参数平台、外部验证和PTE部署准备。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# 20260912_S004_EX17 结论\n\n"
        f"SE冻结体检裁决：`{health.decision.value}`；风险标签：`{health.risk_label.value}`。\n\n"
        "S004-C001保留研究候选身份，但当前不创建S004-v1、不冻结、不进入PTE。"
        "核心原因是搜索偏差证据不利、参数平台偏窄、尚无跨标的验证，并且PTE尚未实现和验证该分钟信号的开盘执行链路。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": protocol["experiment_type"],
        "strategy_id": "S004",
        "symbol": "588080.SH",
        "candidate_id": protocol["candidate_id"],
        "development_cutoff": protocol["development_cutoff"],
        "decision": health.decision.value,
        "risk_label": health.risk_label.value,
        "strategy_frozen": False,
        "pte_mutated": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
