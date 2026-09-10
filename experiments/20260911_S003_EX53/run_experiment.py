from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256
from strategy_evaluator import (
    AuditStatus,
    CandidateReadinessRequest,
    RiskLabel,
    assess_research_candidate,
)


EXPERIMENT_ID = "20260911_S003_EX53"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("creates_strategy_version") or protocol.get("freeze_allowed") or protocol.get("pte_deployment_allowed"):
        raise ValueError("candidate readiness may not freeze or deploy")

    candidate = _read_json(experiment / "candidate_payload.json")
    manifest = _read_json(experiment / "candidate_manifest.json")
    candidate_hash = canonical_json_sha256(candidate)
    if candidate_hash != protocol["candidate_hash"] or candidate_hash != manifest["candidate_hash"]:
        raise ValueError("candidate hash differs from frozen identity")

    source_paths = {
        "synthesis_manifest_sha256": repo / "experiments/20260911_S003_EX52/experiment_manifest.json",
        "synthesis_summary_sha256": repo / "experiments/20260911_S003_EX52/artifacts/synthesis_summary.json",
        "primary_metrics_sha256": repo / "experiments/20260911_S003_EX45/artifacts/mechanism_metrics.csv",
        "external_metrics_sha256": repo / "experiments/20260911_S003_EX51/artifacts/mechanism_metrics.csv",
    }
    for key, path in source_paths.items():
        if _sha256(path) != protocol["source"][key]:
            raise ValueError(f"source hash differs: {path}")
    validate_experiment_archive(repo / "experiments/20260911_S003_EX52")
    synthesis = _read_json(source_paths["synthesis_summary_sha256"])
    primary = pd.read_csv(source_paths["primary_metrics_sha256"]).iloc[0]
    external = pd.read_csv(source_paths["external_metrics_sha256"]).iloc[0]
    requirements = protocol["requirements"]
    labels = {
        "mechanism_evidence": RiskLabel(str(requirements["mechanism_evidence"])),
        "statistical_evidence": RiskLabel(str(requirements["statistical_evidence"])),
        "external_validation": RiskLabel(str(requirements["external_validation"])),
    }
    integrity = bool(
        synthesis["evidence_label"] == "MIXED"
        and synthesis["eligible_for_candidate_workflow"]
        and bool(primary["eligible_for_audit"])
        and bool(external["eligible_for_audit"])
    )
    request = CandidateReadinessRequest(
        candidate_id=str(protocol["candidate_id"]),
        candidate_hash=candidate_hash,
        integrity=AuditStatus.PASS if integrity else AuditStatus.FAIL,
        reproducibility=AuditStatus.PASS,
        mechanism_evidence=labels["mechanism_evidence"],
        statistical_evidence=labels["statistical_evidence"],
        external_validation=labels["external_validation"],
        primary_closed_trades=int(primary["episodes"]),
        minimum_closed_trades=int(requirements["minimum_primary_closed_episodes"]),
        blocking_findings=tuple(map(str, requirements["blocking_findings"])),
    )
    result = assess_research_candidate(request)
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "request": request.to_dict(),
        "result": result.to_dict(),
        "primary_510500": {
            "closed_episodes": int(primary["episodes"]),
            "stress_mean_return": float(primary["stress_mean_return"]),
            "profit_factor": float(primary["stress_profit_factor"]),
            "account_max_drawdown": float(primary["account_max_drawdown"]),
            "account_calmar": float(primary["account_calmar"]),
        },
        "external_512100": {
            "closed_episodes": int(external["episodes"]),
            "stress_mean_return": float(external["stress_mean_return"]),
            "profit_factor": float(external["stress_profit_factor"]),
        },
        "lifecycle_effects": {
            "research_candidate": result.decision.value == "RECOMMEND_REGISTRATION",
            "strategy_version": False,
            "frozen": False,
            "pte_deployed": False,
        },
    }
    _write_json(artifacts / "candidate_readiness.json", evidence)
    decision_cn = {
        "RECOMMEND_REGISTRATION": "建议登记研究候选",
        "INSUFFICIENT_EVIDENCE": "证据不足",
        "REJECT": "拒绝登记",
    }[result.decision.value]
    (experiment / "03_execution.md").write_text(
        "# S003 EX53 执行\n\n状态：COMPLETE。\n\n"
        "已校验候选身份、来源归档、主市场样本和独立横截面复现，并由SE执行研究候选立项审计。"
        "未调用SM、未创建策略版本、未冻结、未修改PTE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX53 结论\n\n状态：COMPLETE。\n\n"
        f"SE裁决：**{decision_cn}**；综合风险标签：`{result.risk_label.value}`。\n\n"
        f"S003-C001在510500上有{int(primary['episodes'])}笔闭合事件，压力收益均值"
        f"{float(primary['stress_mean_return']):.3%}、盈亏比{float(primary['stress_profit_factor']):.2f}；"
        f"在512100上有{int(external['episodes'])}笔独立复现。统计DSR不足保留为MIXED风险。\n\n"
        "该裁决只登记研究候选。正式执行兼容、明确对手PK和冻结前体检仍需分别完成。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": "510500.SH",
            "candidate_id": "S003-C001",
            "development_cutoff": protocol["development_cutoff"],
            "status": "COMPLETE",
            "se_decision": result.decision.value,
            "risk_label": result.risk_label.value,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
