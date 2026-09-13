from __future__ import annotations

import hashlib
import json
from pathlib import Path

from strategy_evaluator import (
    AuditStatus,
    CandidateReadinessRequest,
    RiskLabel,
    assess_research_candidate,
)

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256


EXPERIMENT_ID = "20260913_S001_EX09"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    source = repo / str(protocol["source_experiment"])
    validate_experiment_archive(source)
    payload_path = source / "candidate_payload.json"
    contract_path = source / "artifacts/contract_result.json"
    statistical_path = repo / str(protocol["inherited_statistical_audit"])
    checks = {
        payload_path: str(protocol["candidate_payload_file_sha256"]),
        contract_path: str(protocol["contract_result_sha256"]),
        statistical_path: str(protocol["inherited_statistical_audit_sha256"]),
    }
    for path, expected in checks.items():
        if _sha256(path) != expected:
            raise ValueError(f"source evidence differs from protocol: {path}")
    payload = _read(payload_path)
    contract = _read(contract_path)
    statistical = _read(statistical_path)
    if canonical_json_sha256(payload) != protocol["candidate_hash"]:
        raise ValueError("candidate canonical identity differs from protocol")
    if contract.get("primary_gate") != "PASS" or contract.get("se_replay_audit") != "PASS":
        raise ValueError("candidate formal contract did not pass")
    if statistical.get("risk_label") != protocol["statistical_evidence"]:
        raise ValueError("inherited statistical label differs from protocol")
    monitoring = (repo / str(protocol["monitoring_plan"])).read_text(encoding="utf-8")
    if "状态：`PREPARED`" not in monitoring or "60%" not in monitoring:
        raise ValueError("candidate monitoring plan is incomplete")
    request = CandidateReadinessRequest(
        candidate_id=str(protocol["candidate_id"]),
        candidate_hash=str(protocol["candidate_hash"]),
        integrity=AuditStatus.PASS,
        reproducibility=AuditStatus.PASS,
        mechanism_evidence=RiskLabel(str(protocol["mechanism_evidence"])),
        statistical_evidence=RiskLabel(str(protocol["statistical_evidence"])),
        external_validation=RiskLabel(str(protocol["external_validation"])),
        primary_closed_trades=int(contract["closed_trades"]),
        minimum_closed_trades=int(protocol["minimum_closed_trades"]),
    )
    assessment = assess_research_candidate(request)
    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "request": request.to_dict(),
        "assessment": assessment.to_dict(),
        "additional_correlated_risk_budget_trials": protocol[
            "additional_correlated_risk_budget_trials"
        ],
        "statistical_inheritance_reason": "signal_sequence_unchanged_and_scaling_trials_highly_correlated",
        "route_decision": (
            "PROCEED_TO_BENCHMARK_CHALLENGE"
            if assessment.decision.value == "RECOMMEND_REGISTRATION"
            else "KEEP_RESEARCHING"
        ),
        "strategy_frozen": False,
        "pte_mutated": False,
    }
    _write(artifacts / "candidate_readiness.json", result)
    (experiment / "03_execution.md").write_text(
        "# S001 EX09 执行\n\n"
        f"状态：`COMPLETE`。SE 候选资格裁决 `{assessment.decision.value}`，"
        f"风险标签 `{assessment.risk_label.value}`。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S001 EX09 结论\n\n"
        f"S001-C001 获得正式研究候选资格，统计证据仍为 `{assessment.risk_label.value}`。"
        "该资格只允许进入对手 PK，不允许冻结或加入 PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S001",
            "candidate_id": protocol["candidate_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": result["route_decision"],
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
