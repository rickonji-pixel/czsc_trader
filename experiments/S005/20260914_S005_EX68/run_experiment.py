from __future__ import annotations

import json
from pathlib import Path

from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260914_S005_EX68"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("component_frequency_policy") != "REPORT_ONLY":
        raise ValueError("component frequency must be report-only")
    if protocol.get("reads_target_forward_returns"):
        raise ValueError("EX68 may not read returns")

    corrections: list[dict[str, object]] = []
    for source_spec in protocol["sources"]:
        source = repo / "experiments/S005" / str(source_spec["experiment_id"])
        validate_experiment_archive(source)
        manifest_path = source / "experiment_manifest.json"
        if raw_file_sha256(manifest_path) != source_spec["manifest_sha256"]:
            raise ValueError(f"source manifest differs: {source.name}")
        manifest = _read(manifest_path)
        if manifest.get("decision") != source_spec["original_decision"]:
            raise ValueError(f"source decision differs: {source.name}")
        corrections.append(
            {
                "experiment_id": source.name,
                "original_decision": manifest["decision"],
                "preserved_evidence": ["data", "factor", "event_density"],
                "superseded_interpretation": "STOP_FACTOR_OR_INFORMATION_FAMILY",
                "corrected_scope": source_spec["corrected_scope"],
                "continuous_component_evaluation_allowed": True,
            }
        )

    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "component_frequency_policy": protocol["component_frequency_policy"],
        "complete_strategy_frequency_policy": protocol["complete_strategy_frequency_policy"],
        "corrections": corrections,
        "decision": "PROCEED_TO_CONTINUOUS_COMPONENT_INFORMATION_AUDIT",
        "reads_target_forward_returns": False,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
    }
    (artifacts / "correction_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX68 执行\n\n状态：`COMPLETE`。EX65、EX67原始档案通过哈希与完整性复核，"
        "本实验只纠正裁决适用范围，没有回写历史证据。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX68 结论\n\n裁决：`PROCEED_TO_CONTINUOUS_COMPONENT_INFORMATION_AUDIT`。"
        "组件频率固定为观察指标；完整策略形成后才执行`7/3`硬门。EX65、EX67只证明对应离散事件"
        "不能独立承担中频触发，连续因子的信息价值仍须评估。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": evidence["decision"],
            "reads_target_forward_returns": False,
            "candidate_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
