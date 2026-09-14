from __future__ import annotations

import hashlib
import json
from pathlib import Path

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S006_EX17"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or protocol.get("reads_new_returns"):
        raise ValueError("EX17 protocol identity or return declaration differs")
    evidence_by_id: dict[str, dict[str, object]] = {}
    for source in protocol["sources"]:
        source_experiment = repo / "experiments/S006" / str(source["experiment_id"])
        validate_experiment_archive(source_experiment)
        manifest = source_experiment / "experiment_manifest.json"
        evidence_path = source_experiment / str(source["evidence_path"])
        if _sha256(manifest) != str(source["manifest_sha256"]):
            raise ValueError(f"source manifest differs: {source['experiment_id']}")
        if _sha256(evidence_path) != str(source["evidence_sha256"]):
            raise ValueError(f"source evidence differs: {source['experiment_id']}")
        evidence_by_id[str(source["experiment_id"])] = _read(evidence_path)
    rules = protocol["adjudication"]
    required = {
        "20260914_S006_EX09": rules["required_ex09_decision"],
        "20260914_S006_EX13": rules["required_ex13_decision"],
        "20260914_S006_EX14": rules["required_ex14_decision"],
        "20260914_S006_EX15": rules["required_ex15_decision"],
        "20260914_S006_EX16": rules["required_ex16_decision"],
    }
    for experiment_id, decision in required.items():
        if evidence_by_id[experiment_id].get("decision") != decision:
            raise ValueError(f"adjudication premise differs: {experiment_id}")
    if int(evidence_by_id["20260914_S006_EX16"]["validated_information_families"]) > int(rules["maximum_ex16_validated_information_families"]):
        raise ValueError("conditional evidence no longer supports termination")
    final = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": "TERMINATED_NO_CANDIDATE",
        "reason_codes": [
            "NO_ROBUST_SINGLE_INFORMATION_ANCHOR",
            "ADDITIVE_PLATFORM_NARROW_AND_TOO_COMPLEX",
            "OPC_BOUNDED_ADDITIVE_REPLAYS_FAILED_HARD_GATES",
            "ER60_CONDITIONAL_ARCHITECTURE_LACKS_COMPLEMENTARY_FAMILIES"
        ],
        "reusable_leads": [
            "MARKET_STRUCTURE_IN_TREND",
            "POSITION_VALUATION_IN_RANGE_EXPLORATORY_ONLY"
        ],
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False
    }
    _write(artifacts / "research_line_adjudication.json", final)
    (experiment / "03_execution.md").write_text(
        "# S006 EX17 执行\n\n状态：`COMPLETE`。已校验EX06、EX09、EX12—EX16档案及机器证据；没有读取新收益，没有创建候选或修改SM/PTE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S006 EX17 结论\n\n裁决：`TERMINATED_NO_CANDIDATE`。S006完整保留全目录审计、失败架构及可复用线索；后续新研究不得把S006开发池线索包装成独立证据。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": protocol["experiment_type"],
        "strategy_id": protocol["strategy_id"],
        "symbol": protocol["symbol"],
        "development_cutoff": protocol["development_cutoff"],
        "decision": final["decision"],
        "promotion_allowed": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
