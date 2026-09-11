from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from strategy_evaluator import (
    AuditStatus,
    BenchmarkChallengeDecision,
    FreezeHealthRequest,
    RiskLabel,
    assess_freeze_health,
)

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256, raw_file_sha256


EXPERIMENT_ID = "20260911_S003_EX58"
CANDIDATE_ID = "S003-C001"
CANDIDATE_HASH = "1d6736f816d652c59d43dd22491c567e65100eaddfa3d20575531ca5a124f741"
PTE_BLOCKER = "PTE_INTRADAY_DEPENDENT_EXIT_NOT_IMPLEMENTED"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    artifacts.mkdir(exist_ok=True)
    evidence_paths = {
        "candidate_readiness": repo / "experiments/S003/20260911_S003_EX53/artifacts/candidate_readiness.json",
        "statistical": repo / "experiments/S003/20260911_S003_EX47/artifacts/corrected_statistical_summary.json",
        "cross_market": repo / "experiments/S003/20260911_S003_EX52/artifacts/synthesis_summary.json",
        "formal_replay": repo / "experiments/S003/20260911_S003_EX56/artifacts/formal_replay_summary.json",
        "formal_replay_audit": repo / "experiments/S003/20260911_S003_EX56/artifacts/audit.json",
        "benchmark_challenge": repo / "experiments/S003/20260911_S003_EX57/artifacts/benchmark_assessment.json",
        "monitoring_plan": repo / "research/S003/candidates/S003-C001_MONITORING.md",
    }
    missing = [name for name, path in evidence_paths.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"freeze health evidence is incomplete: {missing}")

    readiness = _read(evidence_paths["candidate_readiness"])
    statistical = _read(evidence_paths["statistical"])
    cross_market = _read(evidence_paths["cross_market"])
    replay = _read(evidence_paths["formal_replay"])
    replay_audit = _read(evidence_paths["formal_replay_audit"])
    challenge = _read(evidence_paths["benchmark_challenge"])
    monitoring_text = evidence_paths["monitoring_plan"].read_text(encoding="utf-8")

    statistical_integrity = bool(
        statistical["status"] == "PASS"
        and abs(
            float(statistical["dsr_effective_probability"])
            - float(cross_market["ex47_dsr_effective_probability"])
        )
        < 1e-12
    )
    identity_pass = {
        str(readiness["request"]["candidate_hash"]),
        str(challenge["request"]["candidate_hash"]),
        CANDIDATE_HASH,
    } == {CANDIDATE_HASH} and statistical_integrity
    replay_checks = replay["checks"]
    reproducibility_pass = bool(
        replay_checks["event_dates_exact"]
        and replay_checks["episode_returns_exact"]
        and replay_checks["candidate_identity_preserved"]
    )
    technical_replay_pass = bool(
        replay["status"] == "PASS"
        and replay_audit["status"] == "PASS"
        and replay_checks["all_cycles_closed"]
        and replay_checks["no_negative_cash"]
    )
    cost_pass = bool(
        float(cross_market["pooled_lower_90"]) > 0
        and cross_market["all_threshold_neighborhood_means_positive"]
    )
    monitoring_pass = "状态：`APPROVED`" in monitoring_text

    conditional_request = FreezeHealthRequest(
        candidate_id=CANDIDATE_ID,
        candidate_hash=CANDIDATE_HASH,
        candidate_readiness=(
            AuditStatus.PASS
            if readiness["result"]["decision"] == "RECOMMEND_REGISTRATION"
            else AuditStatus.FAIL
        ),
        benchmark_challenge=BenchmarkChallengeDecision(str(challenge["result"]["decision"])),
        evidence_integrity=AuditStatus.PASS if identity_pass else AuditStatus.FAIL,
        reproducibility=AuditStatus.PASS if reproducibility_pass else AuditStatus.FAIL,
        technical_replay=AuditStatus.PASS if technical_replay_pass else AuditStatus.FAIL,
        cost_stress=AuditStatus.PASS if cost_pass else AuditStatus.FAIL,
        monitoring_plan=AuditStatus.PASS if monitoring_pass else AuditStatus.FAIL,
        mechanism_evidence=RiskLabel.FAVORABLE,
        statistical_evidence=RiskLabel.MIXED,
        parameter_robustness=RiskLabel.FAVORABLE,
        external_validation=RiskLabel.FAVORABLE,
        execution_evidence=RiskLabel.FAVORABLE,
    )
    conditional_result = assess_freeze_health(conditional_request)
    current_request = replace(conditional_request, blocking_findings=(PTE_BLOCKER,))
    current_result = assess_freeze_health(current_request)
    evidence_index = {
        name: {
            "path": str(path.relative_to(repo)).replace("\\", "/"),
            "sha256": raw_file_sha256(path),
        }
        for name, path in evidence_paths.items()
    }
    source = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "candidate_id": CANDIDATE_ID,
        "candidate_hash": CANDIDATE_HASH,
        "conditional_request": conditional_request.to_dict(),
        "conditional_result": conditional_result.to_dict(),
        "current_request": current_request.to_dict(),
        "current_result": current_result.to_dict(),
        "evidence_index": evidence_index,
        "blocking_findings": [
            {
                "code": PTE_BLOCKER,
                "meaning": "PTE尚不能持久化并恢复开盘买入成交后于11:30卖出等量旧库存的依赖计划",
                "scope": "deployment_engineering",
                "candidate_evidence_affected": False,
            }
        ],
        "risk_disclosures": [
            "全路径有效试验数修正后的DSR概率为11.55%",
            "正式账户口径卡玛为0.27",
            "BuyHold PK的年化收益胜出概率为68.64%，其95%区间跨零",
            "模拟盘证据尚未产生",
        ],
        "lifecycle_effects": {
            "strategy_version_created": False,
            "strategy_frozen": False,
            "pte_deployed": False,
        },
    }
    assessment = {
        "source": source,
        "approval": {
            "schema_version": 1,
            "assessment_id": "FHC-20260911-S003-C001",
            "strategy_id": "S003",
            "candidate_id": CANDIDATE_ID,
            "candidate_hash": CANDIDATE_HASH,
            "decision": current_result.decision.value,
            "risk_label": current_result.risk_label.value,
            "conditional_decision": conditional_result.decision.value,
            "conditional_risk_label": conditional_result.risk_label.value,
            "source_experiment": f"experiments/S003/{EXPERIMENT_ID}",
            "source_hash": canonical_json_sha256(source),
            "assessed_at": "2026-09-11T22:00:00+08:00",
        },
    }
    _write(artifacts / "freeze_health.json", assessment)
    _write(artifacts / "protocol.json", {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "experiment_type": "freeze_health_check",
        "strategy_id": "S003",
        "candidate_id": CANDIDATE_ID,
        "candidate_hash": CANDIDATE_HASH,
        "symbol": "510500.SH",
        "development_cutoff": "2026-09-08",
        "mutates_candidate": False,
        "freezes_strategy": False,
        "mutates_pte": False,
    })
    (experiment / "03_execution.md").write_text(
        "# S003 EX58 执行\n\n状态：COMPLETE。\n\n"
        "SE已复核候选登记、BuyHold PK、身份、可复现性、正式回放、成本压力、监测方案及五类证据。"
        "本实验未修改候选、未冻结策略、未写入PTE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX58 结论\n\n状态：COMPLETE。\n\n"
        f"策略证据条件裁决：`{conditional_result.decision.value}`，风险标签"
        f"`{conditional_result.risk_label.value}`。这说明继续建设PTE具有研究价值。\n\n"
        f"当前生命周期裁决：`{current_result.decision.value}`。唯一阻断项为"
        f"`{PTE_BLOCKER}`：PTE尚不能安全持久化和恢复开盘买入与11:30依赖退出计划。"
        "候选保持研究状态；补齐能力并复验前，不创建版本、不冻结、不进入模拟盘。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": "freeze_health_check",
        "strategy_id": "S003",
        "symbol": "510500.SH",
        "candidate_id": CANDIDATE_ID,
        "development_cutoff": "2026-09-08",
        "decision": current_result.decision.value,
        "conditional_decision": conditional_result.decision.value,
        "risk_label": current_result.risk_label.value,
        "strategy_frozen": False,
        "pte_mutated": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
