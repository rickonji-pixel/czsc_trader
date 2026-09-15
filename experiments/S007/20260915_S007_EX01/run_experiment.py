from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from factor_signal_catalog import CatalogRegistry
from strategy_template_catalog import TemplateRegistry

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX01"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    prohibited = (
        "reads_new_returns",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in prohibited):
        raise ValueError("contract experiment cannot read returns, select or deploy a candidate")

    source_paths = {
        "research_mandate_sha256": repo / "research/RESEARCH_MANDATE.md",
        "s001_benchmark_sha256": repo / "experiments/S001/20260913_S001_EX01/artifacts/gap_summary.json",
        "materials_sha256": repo / "research/S007/materials.json",
    }
    for key, path in source_paths.items():
        if _sha256(path) != str(protocol["sources"][key]):
            raise ValueError(f"frozen source differs: {path}")

    fsc = CatalogRegistry(repo / "catalog")
    stc = TemplateRegistry(repo / "strategy_templates")
    if fsc.digest != protocol["catalogs"]["fsc_digest"]:
        raise ValueError("FSC differs from frozen digest")
    if stc.digest != protocol["catalogs"]["stc_digest"]:
        raise ValueError("STC differs from frozen digest")

    hypotheses = protocol.get("hypotheses")
    if not isinstance(hypotheses, list) or len(hypotheses) != 5:
        raise ValueError("exactly five first-principles hypothesis families are required")
    ids = [str(item["hypothesis_id"]) for item in hypotheses]
    if len(ids) != len(set(ids)):
        raise ValueError("hypothesis IDs must be unique")

    ledger_path = artifacts / "hypothesis_ledger.csv"
    fields = [
        "hypothesis_id",
        "economic_question",
        "competitive_explanations",
        "minimum_data",
        "current_disposition",
    ]
    with ledger_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in hypotheses:
            writer.writerow(
                {
                    "hypothesis_id": item["hypothesis_id"],
                    "economic_question": item["economic_question"],
                    "competitive_explanations": " | ".join(item["competitive_explanations"]),
                    "minimum_data": " | ".join(item["minimum_data"]),
                    "current_disposition": "PENDING_DATA_AND_CAUSALITY_AUDIT",
                }
            )

    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": "PROCEED_TO_DATA_AND_CAUSALITY_AUDIT",
        "hypothesis_count": len(hypotheses),
        "hypothesis_ids": ids,
        "fsc_digest": fsc.digest,
        "fsc_definition_count": len(fsc.list_definitions()),
        "stc_digest": stc.digest,
        "stc_template_count": len(stc.templates),
        "input_selected": False,
        "template_selected": False,
        "search_started": False,
        "new_return_paths_read": 0,
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "contract_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S007 EX01 执行记录\n\n"
        "研究合同、五类竞争假设、FSC与STC身份均已校验。没有选择输入、模板或参数，"
        "没有读取新的策略收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX01 结论\n\n"
        "裁决：`PROCEED_TO_DATA_AND_CAUSALITY_AUDIT`。S007从资产属性重新建立五类假设，"
        "不继承S005/S006的研究选择；当前没有Alpha、候选或模板结论。\n",
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
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
