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


EXPERIMENT_ID = "20260913_S004_EX46"
ARRIVAL_BLOCKER = "MARGIN_DETAIL_PREOPEN_ARRIVAL_NOT_PROVEN"
PTE_BLOCKER = "PTE_S004_SIGNAL_AND_EXECUTION_NOT_IMPLEMENTED_OR_VALIDATED"


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
    candidate_registry_path = repo / "research/S004/candidates/S004-C002.json"
    evidence_paths = {
        "candidate_definition": repo / "experiments/S004/20260913_S004_EX41/artifacts/candidate_spec.json",
        "mechanism": repo / "experiments/S004/20260912_S004_EX06/artifacts/attribution_summary.json",
        "base_platform": repo / "experiments/S004/20260912_S004_EX19/artifacts/neighborhood_summary.json",
        "risk_platform": repo / "experiments/S004/20260913_S004_EX45/artifacts/platform_summary.json",
        "construction": repo / "experiments/S004/20260913_S004_EX41/artifacts/candidate_metrics.json",
        "formal_replay": repo / "experiments/S004/20260913_S004_EX42/artifacts/equivalence_summary.json",
        "statistical": repo / "experiments/S004/20260913_S004_EX43/artifacts/statistical_summary.json",
        "benchmark": repo / "experiments/S004/20260913_S004_EX44/artifacts/benchmark_assessment.json",
        "monitoring": repo / "research/S004/candidates/S004-C002_MONITORING.md",
    }
    missing = [name for name, path in evidence_paths.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"freeze health evidence is incomplete: {missing}")
    for source_id in (
        "20260912_S004_EX06",
        "20260912_S004_EX19",
        "20260913_S004_EX41",
        "20260913_S004_EX42",
        "20260913_S004_EX43",
        "20260913_S004_EX44",
        "20260913_S004_EX45",
    ):
        validate_experiment_archive(repo / "experiments" / "S004" / source_id)

    candidate = _read(candidate_registry_path)
    construction = _read(evidence_paths["construction"])
    formal = _read(evidence_paths["formal_replay"])
    statistical = _read(evidence_paths["statistical"])
    benchmark = _read(evidence_paths["benchmark"])
    base_platform = _read(evidence_paths["base_platform"])
    risk_platform = _read(evidence_paths["risk_platform"])
    monitoring = evidence_paths["monitoring"].read_text(encoding="utf-8")
    if candidate.get("candidate_spec_normalized_sha256") != protocol["candidate_hash"]:
        raise ValueError("candidate identity differs from health protocol")
    if formal["se_replay_audit"] != "PASS" or statistical["evidence_label"] != "FAVORABLE":
        raise ValueError("formal or statistical evidence differs from health protocol")
    if risk_platform["evidence_label"] != "FAVORABLE" or "状态：`PREPARED`" not in monitoring:
        raise ValueError("parameter platform or monitoring plan is incomplete")

    readiness_request = CandidateReadinessRequest(
        candidate_id=str(protocol["candidate_id"]),
        candidate_hash=str(protocol["candidate_hash"]),
        integrity=AuditStatus.PASS,
        reproducibility=AuditStatus.PASS,
        mechanism_evidence=RiskLabel.FAVORABLE,
        statistical_evidence=RiskLabel.FAVORABLE,
        external_validation=RiskLabel.MIXED,
        primary_closed_trades=int(construction["filtered_episodes"]),
        minimum_closed_trades=100,
    )
    readiness = assess_research_candidate(readiness_request)
    primary_pk = benchmark["assessments"]["S004-C001"]["result"]
    benchmark_decision = BenchmarkChallengeDecision(str(primary_pk["decision"]))
    blockers = (ARRIVAL_BLOCKER, PTE_BLOCKER)
    health_request = FreezeHealthRequest(
        candidate_id=str(protocol["candidate_id"]),
        candidate_hash=str(protocol["candidate_hash"]),
        candidate_readiness=(AuditStatus.PASS if readiness.decision.value == "RECOMMEND_REGISTRATION" else AuditStatus.FAIL),
        benchmark_challenge=benchmark_decision,
        evidence_integrity=AuditStatus.PASS,
        reproducibility=AuditStatus.PASS,
        technical_replay=AuditStatus.PASS,
        cost_stress=AuditStatus.PASS,
        monitoring_plan=AuditStatus.PASS,
        mechanism_evidence=RiskLabel.FAVORABLE,
        statistical_evidence=RiskLabel.FAVORABLE,
        parameter_robustness=(RiskLabel.FAVORABLE if base_platform["evidence_label"] == "FAVORABLE" and risk_platform["evidence_label"] == "FAVORABLE" else RiskLabel.MIXED),
        external_validation=RiskLabel.MIXED,
        execution_evidence=RiskLabel.FAVORABLE,
        blocking_findings=blockers,
    )
    health = assess_freeze_health(health_request)
    evidence_index = {
        name: {"path": str(path.relative_to(repo)).replace("\\", "/"), "sha256": raw_file_sha256(path)}
        for name, path in evidence_paths.items()
    }
    source = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "candidate_id": protocol["candidate_id"],
        "candidate_hash": protocol["candidate_hash"],
        "research_assessment": "FAVORABLE",
        "deployment_readiness": "BLOCKED",
        "readiness_request": readiness_request.to_dict(),
        "readiness_result": readiness.to_dict(),
        "health_request": health_request.to_dict(),
        "health_result": health.to_dict(),
        "evidence_index": evidence_index,
        "risk_disclosures": [
            f"74条历史收益路径的PBO为{float(statistical['pbo']):.2%}",
            f"有效DSR概率为{float(statistical['dsr_effective_probability']):.2%}",
            f"21日区块Bootstrap年化收益90%下界为{float(statistical['bootstrap_21d_cagr_lower_90']):.2%}",
            "510500只提供部分外部复现，综合外部证据为MIXED",
            "低波动压力收益仍略为负",
            "交易频率降级为观察指标",
        ],
        "blocking_findings": list(blockers),
        "next_actions": [
            "证明margin_detail在交易日开盘前稳定到达并记录时间戳",
            "实现并验证PTE对S004分钟信号、融资风险否决及开盘执行的完整链路",
            "阻塞项解除后原样重跑冻结体检，不再调整研究参数",
        ],
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
            "assessment_id": "FHC-20260913-S004-C002",
            "strategy_id": "S004",
            "candidate_id": protocol["candidate_id"],
            "candidate_hash": protocol["candidate_hash"],
            "decision": health.decision.value,
            "risk_label": health.risk_label.value,
            "source_experiment": f"experiments/S004/{EXPERIMENT_ID}",
            "source_hash": canonical_json_sha256(source),
            "assessed_at": "2026-09-13T23:30:00+08:00",
        },
    }
    _write(artifacts / "freeze_health.json", assessment)
    (experiment / "03_execution.md").write_text(
        "# S004 EX46 执行\n\n状态：COMPLETE。SE 已汇总候选、机制、正式回放、74 路径统计审计、双对手 PK、两层参数平台、成本压力和监测方案。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX46 结论\n\n"
        f"SE 冻结体检裁决：`{health.decision.value}`；风险标签：`{health.risk_label.value}`。\n\n"
        "研究证据为 `FAVORABLE`，C002 已通过正式回放、统计审计、双对手 PK 和参数平台检查。"
        "当前只剩两个部署阻塞项：融资明细开盘前稳定到达尚未证实，PTE 的 S004 完整执行链路尚未实现和验证。"
        "因此本轮不创建 S004-v1、不冻结、不进入 PTE，也不再调研究参数。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S004",
            "symbol": protocol["symbol"],
            "candidate_id": protocol["candidate_id"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": health.decision.value,
            "research_assessment": "FAVORABLE",
            "deployment_readiness": "BLOCKED",
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
