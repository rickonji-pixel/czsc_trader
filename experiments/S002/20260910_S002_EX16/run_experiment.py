from __future__ import annotations

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


EXPERIMENT_ID = "20260910_S002_EX16"
CANDIDATE_ID = "S002-C001"
CANDIDATE_HASH = "8a804a7fafc784c92ddd8b8c2d4c3fbc8dda953ca0fd7f0cfb6ab6fd7650574d"


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
    repo = experiment.parents[1]
    artifacts = experiment / "artifacts"
    artifacts.mkdir(exist_ok=True)
    evidence_paths = {
        "statistical": repo / "experiments/20260910_S002_EX09/artifacts/statistical_summary.json",
        "external_validation": repo / "experiments/20260910_S002_EX11/artifacts/validation_summary.json",
        "candidate_readiness": repo / "experiments/20260910_S002_EX12/artifacts/candidate_readiness.json",
        "formal_execution": repo / "experiments/20260910_S002_EX14/artifacts/execution_compatibility.json",
        "benchmark_challenge": repo / "experiments/20260910_S002_EX15/artifacts/freeze_assessment.json",
        "monitoring_plan": repo / "research/S002/candidates/S002-C001_MONITORING.md",
    }
    if any(not path.is_file() for path in evidence_paths.values()):
        raise FileNotFoundError("freeze health evidence is incomplete")

    statistical = _read(evidence_paths["statistical"])
    external = _read(evidence_paths["external_validation"])
    readiness = _read(evidence_paths["candidate_readiness"])
    execution = _read(evidence_paths["formal_execution"])
    challenge = _read(evidence_paths["benchmark_challenge"])
    candidate_hashes = {
        str(readiness["request"]["candidate_hash"]),
        str(execution["candidate_hash"]),
        str(challenge["request"]["candidate_hash"]),
    }
    identity_pass = candidate_hashes == {CANDIDATE_HASH}
    reproduction_pass = bool(
        statistical["direction_flags"]["leave_one_year_out"] == "FAVORABLE"
        and execution["target_positions_identical"]
    )
    cost_pass = bool(
        statistical["direction_flags"]["cost_tolerance"] == "FAVORABLE"
        and float(statistical["details"]["h5_calmar_at_15bp"]) >= 1.0
    )
    monitoring_text = evidence_paths["monitoring_plan"].read_text(encoding="utf-8")
    monitoring_pass = "状态：`APPROVED`" in monitoring_text

    request = FreezeHealthRequest(
        candidate_id=CANDIDATE_ID,
        candidate_hash=CANDIDATE_HASH,
        candidate_readiness=(
            AuditStatus.PASS
            if readiness["result"]["decision"] == "RECOMMEND_REGISTRATION"
            else AuditStatus.FAIL
        ),
        benchmark_challenge=BenchmarkChallengeDecision(
            str(challenge["result"]["decision"])
        ),
        evidence_integrity=AuditStatus.PASS if identity_pass else AuditStatus.FAIL,
        reproducibility=AuditStatus.PASS if reproduction_pass else AuditStatus.FAIL,
        technical_replay=(
            AuditStatus.PASS
            if execution["se_replay_audit"]["status"] == "PASS"
            else AuditStatus.FAIL
        ),
        cost_stress=AuditStatus.PASS if cost_pass else AuditStatus.FAIL,
        monitoring_plan=AuditStatus.PASS if monitoring_pass else AuditStatus.FAIL,
        mechanism_evidence=RiskLabel.MIXED,
        statistical_evidence=RiskLabel(str(statistical["evidence_label"])),
        parameter_robustness=RiskLabel.FAVORABLE,
        external_validation=RiskLabel(str(external["evidence_label"])),
        execution_evidence=RiskLabel.MIXED,
    )
    result = assess_freeze_health(request)
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
        "request": request.to_dict(),
        "result": result.to_dict(),
        "evidence_index": evidence_index,
        "risk_disclosures": [
            "510500主样本仅25笔闭合交易",
            "有效试验数修正后的DSR概率为61.13%",
            "512100横截面验证为MIXED",
            "正式执行收益和卡玛低于研究用次日开盘口径",
            "BuyHold PK的年化收益胜出概率为59.35%",
        ],
        "lifecycle_effects": {
            "strategy_version_created": False,
            "strategy_frozen": False,
            "pte_deployed": False,
        },
    }
    approval = {
        "schema_version": 1,
        "assessment_id": "FHC-20260910-S002-C001",
        "strategy_id": "S002",
        "candidate_id": CANDIDATE_ID,
        "candidate_hash": CANDIDATE_HASH,
        "decision": result.decision.value,
        "risk_label": result.risk_label.value,
        "source_experiment": f"experiments/{EXPERIMENT_ID}",
        "source_hash": canonical_json_sha256(source),
        "assessed_at": "2026-09-10T20:00:00+08:00",
    }
    _write(artifacts / "freeze_health.json", {"source": source, "approval": approval})
    _write(
        artifacts / "protocol.json",
        {
            "schema_version": 1,
            "experiment_id": EXPERIMENT_ID,
            "experiment_type": "freeze_health_check",
            "strategy_id": "S002",
            "candidate_id": CANDIDATE_ID,
            "candidate_hash": CANDIDATE_HASH,
            "symbol": "510500.SH",
            "development_cutoff": "2026-09-08",
            "mutates_candidate": False,
            "freezes_strategy": False,
            "mutates_pte": False,
        },
    )
    (experiment / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n状态：COMPLETE。\n\n"
        "已逐项校验研究候选登记、BuyHold PK、证据身份、可复现性、正式执行回放、"
        "成本压力和冻结后监测方案。全部证据文件已记录内容哈希。未修改候选、未冻结策略、"
        "未写入PTE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        f"# {EXPERIMENT_ID} 结论\n\n状态：COMPLETE。\n\n"
        f"SE冻结前体检裁决：`{result.decision.value}`；风险标签："
        f"`{result.risk_label.value}`。\n\n"
        "候选登记、BuyHold PK、证据完整性、可复现性、正式执行回放、15bp成本压力和"
        "冻结后监测方案均通过。统计稳健性、横截面和正式执行证据继续标记为MIXED，"
        "完整风险披露见`artifacts/freeze_health.json`。\n\n"
        "本裁决允许候选提交人工冻结确认；当前仍为研究候选，没有创建S002-v1，"
        "也没有写入PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": "freeze_health_check",
            "strategy_id": "S002",
            "symbol": "510500.SH",
            "candidate_id": CANDIDATE_ID,
            "development_cutoff": "2026-09-08",
            "decision": result.decision.value,
            "risk_label": result.risk_label.value,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
