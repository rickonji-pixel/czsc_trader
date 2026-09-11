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

from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.identity import canonical_json_sha256, raw_file_sha256


EXPERIMENT_ID = "20260911_S003_EX60"
CANDIDATE_ID = "S003-C001"
CANDIDATE_HASH = "1d6736f816d652c59d43dd22491c567e65100eaddfa3d20575531ca5a124f741"


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
    ex58_path = repo / "experiments/S003/20260911_S003_EX58/artifacts/freeze_health.json"
    ex59_path = repo / "experiments/S003/20260911_S003_EX59/artifacts/pte_compatibility.json"
    ex58 = _read(ex58_path)
    ex59 = _read(ex59_path)
    source58 = ex58["source"]
    if not isinstance(source58, dict):
        raise ValueError("EX58 source is invalid")
    previous = source58["conditional_request"]
    if not isinstance(previous, dict):
        raise ValueError("EX58 conditional request is invalid")
    if previous.get("candidate_id") != CANDIDATE_ID:
        raise ValueError("EX58 candidate id differs")
    if previous.get("candidate_hash") != CANDIDATE_HASH:
        raise ValueError("EX58 candidate hash differs")
    if ex59.get("status") != "PASS" or ex59.get("candidate_hash") != CANDIDATE_HASH:
        raise ValueError("PTE compatibility evidence is not valid for this candidate")

    identities = ex59.get("implementation_identity")
    if not isinstance(identities, dict) or not identities:
        raise ValueError("PTE implementation identity is missing")
    drift = {
        str(path): {"recorded": expected, "current": raw_file_sha256(repo / str(path))}
        for path, expected in identities.items()
        if raw_file_sha256(repo / str(path)) != expected
    }
    if drift:
        raise ValueError(f"PTE implementation changed after EX59: {drift}")

    request = FreezeHealthRequest(
        candidate_id=CANDIDATE_ID,
        candidate_hash=CANDIDATE_HASH,
        candidate_readiness=AuditStatus(str(previous["candidate_readiness"])),
        benchmark_challenge=BenchmarkChallengeDecision(
            str(previous["benchmark_challenge"])
        ),
        evidence_integrity=AuditStatus(str(previous["evidence_integrity"])),
        reproducibility=AuditStatus(str(previous["reproducibility"])),
        technical_replay=AuditStatus(str(previous["technical_replay"])),
        cost_stress=AuditStatus(str(previous["cost_stress"])),
        monitoring_plan=AuditStatus(str(previous["monitoring_plan"])),
        mechanism_evidence=RiskLabel(str(previous["mechanism_evidence"])),
        statistical_evidence=RiskLabel(str(previous["statistical_evidence"])),
        parameter_robustness=RiskLabel(str(previous["parameter_robustness"])),
        external_validation=RiskLabel(str(previous["external_validation"])),
        execution_evidence=RiskLabel.FAVORABLE,
        blocking_findings=(),
    )
    result = assess_freeze_health(request)
    if result.decision.value != "RECOMMEND_FREEZE":
        raise AssertionError(f"unexpected freeze health decision: {result.to_dict()}")

    source = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "candidate_id": CANDIDATE_ID,
        "candidate_hash": CANDIDATE_HASH,
        "request": request.to_dict(),
        "result": result.to_dict(),
        "evidence_index": {
            "previous_health": {
                "path": str(ex58_path.relative_to(repo)).replace("\\", "/"),
                "sha256": raw_file_sha256(ex58_path),
            },
            "pte_compatibility": {
                "path": str(ex59_path.relative_to(repo)).replace("\\", "/"),
                "sha256": raw_file_sha256(ex59_path),
            },
        },
        "resolved_blocking_findings": [
            "PTE_INTRADAY_DEPENDENT_EXIT_NOT_IMPLEMENTED"
        ],
        "remaining_risk_disclosures": source58["risk_disclosures"],
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
            "assessment_id": "FHC-20260911-S003-C001-R2",
            "strategy_id": "S003",
            "candidate_id": CANDIDATE_ID,
            "candidate_hash": CANDIDATE_HASH,
            "decision": result.decision.value,
            "risk_label": result.risk_label.value,
            "source_experiment": f"experiments/S003/{EXPERIMENT_ID}",
            "source_hash": canonical_json_sha256(source),
            "assessed_at": "2026-09-11T23:30:00+08:00",
        },
    }
    _write(artifacts / "freeze_health.json", assessment)
    _write(artifacts / "protocol.json", {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "experiment_type": "freeze_health_recheck",
        "candidate_id": CANDIDATE_ID,
        "candidate_hash": CANDIDATE_HASH,
        "development_cutoff": "2026-09-08",
        "freezes_strategy": False,
        "mutates_pte": False,
    })
    (experiment / "03_execution.md").write_text(
        "# S003 EX60 执行\n\n状态：COMPLETE。\n\n"
        "SE复用EX58策略证据并核验EX59的PTE兼容证据及实现哈希。"
        "实验未创建版本、未冻结策略、未写入PTE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX60 结论\n\n状态：COMPLETE。\n\n"
        f"当前生命周期裁决：`{result.decision.value}`，风险标签"
        f"`{result.risk_label.value}`。EX58唯一工程阻断已解除，S003-C001具备提交人工冻结"
        "确认的资格。统计幸运性、正式账户卡玛和模拟盘证据缺失仍按原口径披露。"
        "本裁决不等于已经冻结或部署。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": "freeze_health_recheck",
        "strategy_id": "S003",
        "symbol": "510500.SH",
        "candidate_id": CANDIDATE_ID,
        "development_cutoff": "2026-09-08",
        "decision": result.decision.value,
        "risk_label": result.risk_label.value,
        "strategy_frozen": False,
        "pte_mutated": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
