from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from strategy_evaluator import (
    AuditStatus,
    CandidateReadinessRequest,
    RiskLabel,
    assess_research_candidate,
)


EXPERIMENT_ID = "20260910_S002_EX12"


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_protocol(protocol: dict[str, object]) -> None:
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("creates_strategy_version"):
        raise ValueError("research-candidate assessment may not create a version")
    if protocol.get("freeze_allowed") or protocol.get("pte_deployment_allowed"):
        raise ValueError("research-candidate assessment may not freeze or deploy")


def _validate_sources(repo_root: Path, protocol: dict[str, object]) -> None:
    for source in protocol["source_experiments"].values():
        path = repo_root / "experiments" / str(source["experiment_id"])
        validate_experiment_archive(path)
        if _sha256(path / "experiment_manifest.json") != source["manifest_sha256"]:
            raise ValueError(f"source manifest hash differs: {path.name}")


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo_root = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    _validate_protocol(protocol)
    _validate_sources(repo_root, protocol)

    manifest = _read_json(experiment / str(protocol["candidate_manifest"]))
    payload = _read_json(experiment / str(manifest["payload_path"]))
    candidate_hash = _canonical_hash(payload)
    if candidate_hash != manifest["candidate_hash"]:
        raise ValueError("candidate payload hash differs from frozen manifest")
    if payload["candidate_id"] != protocol["candidate_id"]:
        raise ValueError("candidate identity differs from frozen protocol")

    ex07 = repo_root / "experiments" / "20260909_S002_EX07"
    metrics = pd.read_csv(ex07 / "artifacts" / "window_metrics.csv")
    selected = metrics.loc[
        metrics["holding_sessions"].eq(5)
        & metrics["window"].eq("2021_2026YTD")
    ]
    if len(selected) != 1:
        raise ValueError("primary five-session metrics are missing or duplicated")
    primary = selected.iloc[0]
    ex05_metrics = pd.read_csv(
        repo_root / "experiments" / "20260909_S002_EX05"
        / "artifacts" / "window_metrics.csv"
    )
    buyhold_rows = ex05_metrics.loc[
        ex05_metrics["subject"].eq("BUYHOLD")
        & ex05_metrics["window"].eq("2021_2026YTD")
    ]
    if len(buyhold_rows) != 1:
        raise ValueError("BuyHold benchmark is missing or duplicated")
    buyhold = buyhold_rows.iloc[0]

    identifiability = _read_json(
        repo_root / "experiments" / "20260910_S002_EX08"
        / "artifacts" / "identifiability_summary.json"
    )
    statistics = _read_json(
        repo_root / "experiments" / "20260910_S002_EX09"
        / "artifacts" / "statistical_summary.json"
    )
    reproduction = _read_json(
        repo_root / "experiments" / "20260910_S002_EX09"
        / "artifacts" / "run_evidence.json"
    )
    external = _read_json(
        repo_root / "experiments" / "20260910_S002_EX11"
        / "artifacts" / "validation_summary.json"
    )
    labels = {
        "mechanism_evidence": str(identifiability["evidence_label"]),
        "statistical_evidence": str(statistics["evidence_label"]),
        "external_validation": str(external["evidence_label"]),
    }
    source_specs = protocol["source_experiments"]
    for name, source_name in (
        ("mechanism_evidence", "identifiability"),
        ("statistical_evidence", "statistics"),
        ("external_validation", "cross_sectional"),
    ):
        if labels[name] != source_specs[source_name]["required_label"]:
            raise ValueError(f"{name} differs from frozen protocol")

    requirements = protocol["requirements"]
    request = CandidateReadinessRequest(
        candidate_id=str(protocol["candidate_id"]),
        candidate_hash=candidate_hash,
        integrity=AuditStatus.PASS,
        reproducibility=(
            AuditStatus.PASS
            if reproduction["source_reproduction_passed"]
            else AuditStatus.FAIL
        ),
        mechanism_evidence=RiskLabel(labels["mechanism_evidence"]),
        statistical_evidence=RiskLabel(labels["statistical_evidence"]),
        external_validation=RiskLabel(labels["external_validation"]),
        primary_closed_trades=int(primary["closed_trades"]),
        minimum_closed_trades=int(requirements["minimum_primary_closed_trades"]),
        blocking_findings=tuple(map(str, requirements["blocking_findings"])),
    )
    result = assess_research_candidate(request)
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "request": request.to_dict(),
        "result": result.to_dict(),
        "primary_metrics": {
            "window": str(primary["window"]),
            "start": str(primary["start"]),
            "end": str(primary["end"]),
            "closed_trades": int(primary["closed_trades"]),
            "max_drawdown": float(primary["max_drawdown"]),
            "calmar": float(primary["calmar"]),
            "profit_factor": float(primary["win_loss_ratio"]),
            "total_return": float(primary["return"]),
        },
        "diagnostic_benchmark": {
            "subject": "BuyHold",
            "affects_registration": False,
            "window": str(buyhold["window"]),
            "start": str(buyhold["start"]),
            "end": str(buyhold["end"]),
            "max_drawdown": float(buyhold["max_drawdown"]),
            "calmar": float(buyhold["calmar"]),
            "profit_factor": None,
            "total_return": float(buyhold["return"]),
        },
        "lifecycle_effects": {
            "research_candidate": result.decision.value == "RECOMMEND_REGISTRATION",
            "strategy_version": False,
            "frozen": False,
            "pte_deployed": False,
        }
    }
    _write_json(artifacts / "candidate_readiness.json", evidence)

    decision_cn = {
        "RECOMMEND_REGISTRATION": "建议登记研究候选",
        "INSUFFICIENT_EVIDENCE": "证据不足",
        "REJECT": "拒绝登记",
    }[result.decision.value]
    (experiment / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n状态：COMPLETE。\n\n"
        "已验证四个来源实验的完整档案和冻结哈希，复核候选payload身份，并由SE完成首个"
        "研究候选立项审计。未调用SM、未创建策略版本、未冻结、未修改PTE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        f"# {EXPERIMENT_ID} 结论\n\n状态：COMPLETE。\n\n"
        f"SE裁决：**{decision_cn}**；综合风险标签：`{result.risk_label.value}`。\n\n"
        f"S002-C001主样本共有{int(primary['closed_trades'])}笔闭合交易，最大回撤"
        f"{float(primary['max_drawdown']):.2%}，卡玛{float(primary['calmar']):.4f}，"
        f"盈亏比{float(primary['win_loss_ratio']):.4f}，累计收益"
        f"{float(primary['return']):.2%}。机制可辨识性、统计稳健性和横截面验证均为"
        "`MIXED`，没有触发立项拒绝条件。\n\n"
        f"同窗口BuyHold累计收益{float(buyhold['return']):.2%}、最大回撤"
        f"{float(buyhold['max_drawdown']):.2%}、卡玛{float(buyhold['calmar']):.4f}。"
        "该参照只用于解释收益与风险交换，不影响SE立项裁决。\n\n"
        "该裁决只表示机制值得作为正式研究候选继续存在。S002仍处于RESEARCH，当前没有"
        "策略版本、冻结资格或PTE部署资格。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S002",
            "symbol": "510500.SH",
            "candidate_id": "S002-C001",
            "development_cutoff": protocol["development_cutoff"],
            "se_decision": result.decision.value,
            "risk_label": result.risk_label.value,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
