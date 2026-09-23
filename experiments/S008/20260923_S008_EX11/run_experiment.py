from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from factor_signal_catalog import CatalogRegistry
from strategy_template_catalog import TemplateRegistry

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX11"


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
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    prohibited = (
        "reads_new_returns",
        "selects_input",
        "selects_template",
        "starts_search",
        "candidate_generation",
        "reads_sealed_validation",
        "mutates_platform",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in prohibited):
        raise ValueError("research contract cannot select, search, promote or mutate")

    source_paths = {
        "family_sha256": repo / "research/registrations/S008/family.json",
        "materials_sha256": repo / "research/S008/materials.json",
    }
    for key, path in source_paths.items():
        if _sha256(path) != str(protocol["sources"][key]):
            raise ValueError(f"frozen source differs: {path}")

    family = _read(source_paths["family_sha256"])
    intent = family.get("research_intent")
    if not isinstance(intent, dict):
        raise ValueError("registered research intent is missing")
    objective = intent.get("evaluation_objective")
    if not isinstance(objective, dict):
        raise ValueError("registered evaluation objective is missing")
    hard_gates = objective.get("hard_gates")
    if not isinstance(hard_gates, dict) or len(hard_gates) != 2:
        raise ValueError("exactly two user-confirmed hard gates are required")

    fsc = CatalogRegistry(repo / "catalog")
    stc = TemplateRegistry(repo / "strategy_templates")
    if fsc.digest != protocol["catalogs"]["fsc_digest"]:
        raise ValueError("FSC differs from frozen digest")
    if stc.digest != protocol["catalogs"]["stc_digest"]:
        raise ValueError("STC differs from frozen digest")

    hypotheses = protocol.get("hypotheses")
    if not isinstance(hypotheses, list) or len(hypotheses) != 6:
        raise ValueError("exactly six first-principles hypothesis families are required")
    ids = [str(item["hypothesis_id"]) for item in hypotheses]
    if len(ids) != len(set(ids)):
        raise ValueError("hypothesis IDs must be unique")
    required = {
        "economic_question",
        "risk_preference_behavior",
        "competitive_explanations",
        "falsification_condition",
        "minimum_data",
    }
    if any(not required.issubset(item) for item in hypotheses):
        raise ValueError("hypothesis contract is incomplete")

    fields = [
        "hypothesis_id",
        "economic_question",
        "risk_preference_behavior",
        "competitive_explanations",
        "falsification_condition",
        "minimum_data",
        "current_disposition",
    ]
    with (artifacts / "hypothesis_ledger.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in hypotheses:
            writer.writerow(
                {
                    "hypothesis_id": item["hypothesis_id"],
                    "economic_question": item["economic_question"],
                    "risk_preference_behavior": item["risk_preference_behavior"],
                    "competitive_explanations": " | ".join(item["competitive_explanations"]),
                    "falsification_condition": item["falsification_condition"],
                    "minimum_data": " | ".join(item["minimum_data"]),
                    "current_disposition": "PENDING_DATA_AND_CAUSALITY_AUDIT",
                }
            )

    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": "PROCEED_TO_DATA_INTERFACE_AND_CAUSALITY_AUDIT",
        "hard_gate_count": 2,
        "observation_metric_count": 2,
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
        "sealed_validation_read": False,
        "candidate_created": False,
        "platform_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "contract_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S008 EX11 执行记录\n\n"
        "用户确认的两项硬门、两项观察指标、六类黄金经济机制、交易者风险偏好、竞争解释与"
        "最低数据需求均已校验。没有读取新增收益、选择输入或模板、启动搜索或读取封存池。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX11 结论\n\n"
        "裁决：`PROCEED_TO_DATA_INTERFACE_AND_CAUSALITY_AUDIT`。S008已从价格规则筛选切换为"
        "第一性原理的多源机制研究；当前没有Alpha、候选、输入或原型结论。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "credential_id": protocol["credential_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": evidence["decision"],
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
