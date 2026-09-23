from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX19"


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    prohibited = (
        "reads_returns",
        "starts_search",
        "selects_winner",
        "candidate_generation",
        "reads_sealed_validation",
        "mutates_catalog",
        "mutates_platform",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in prohibited):
        raise ValueError("prototype preregistration cannot read returns, search, select or mutate")

    ex18 = repo / "experiments/S008/20260923_S008_EX18"
    ex16 = repo / "experiments/S008/20260923_S008_EX16"
    validate_experiment_archive(ex18)
    validate_experiment_archive(ex16)
    sources = {
        "ex18_manifest_sha256": ex18 / "experiment_manifest.json",
        "component_panel_sha256": ex18 / "artifacts/component_panel.csv",
        "feature_catalog_sha256": ex16 / "artifacts/feature_catalog.csv",
    }
    for key, path in sources.items():
        if _sha256(path) != protocol["sources"][key]:
            raise ValueError(f"frozen source differs: {path}")

    component_panel = pd.read_csv(sources["component_panel_sha256"])
    catalog = pd.read_csv(sources["feature_catalog_sha256"])
    groups = protocol["component_groups"]
    grouped_features = [feature for values in groups.values() for feature in values]
    if len(grouped_features) != 13 or len(set(grouped_features)) != 13:
        raise ValueError("component groups must contain 13 unique features")
    if set(grouped_features) != set(component_panel["feature"]):
        raise ValueError("prototype component groups differ from EX18 panel")
    if not set(grouped_features).issubset(set(catalog["feature"])):
        raise ValueError("prototype component missing from causal feature catalog")

    prototypes = protocol["prototypes"]
    prototype_ids = [item["prototype_id"] for item in prototypes]
    if len(prototypes) != 3 or len(set(prototype_ids)) != 3:
        raise ValueError("exactly three distinct prototypes are required")
    structures = {item["structure"] for item in prototypes}
    if len(structures) != 3:
        raise ValueError("prototype structures must be materially distinct")
    execution = protocol["execution_contract"]
    if execution["allowed_target_positions"] != [0.0, 1.0]:
        raise ValueError("position contract differs from user confirmation")
    if execution["decision_time"] != "T_CLOSE" or execution["execution_time"] != "T_PLUS_1_OPEN":
        raise ValueError("decision or execution timing differs from user confirmation")
    if execution["initial_cash"] != 1_000_000.0:
        raise ValueError("initial cash differs from user confirmation")
    if execution["primary_one_way_cost_bps"] != 10.0 or execution["stress_one_way_cost_bps"] != 30.0:
        raise ValueError("cost contract differs from user confirmation")
    hard_gates = protocol["evaluation_contract"]["hard_gates"]
    if set(hard_gates) != {"annualized_return", "maximum_drawdown_magnitude"}:
        raise ValueError("only the two user-confirmed hard gates are allowed")

    registry = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "normalization": protocol["normalization"],
        "execution_contract": execution,
        "evaluation_contract": protocol["evaluation_contract"],
        "component_groups": groups,
        "prototypes": prototypes,
    }
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "decision": "PROCEED_TO_PROTOTYPE_IMPLEMENTATION_AND_EXPRESSIBILITY_GATE",
        "component_count": len(grouped_features),
        "prototype_count": len(prototypes),
        "prototype_ids": prototype_ids,
        "structures": sorted(structures),
        "hard_gates": sorted(hard_gates),
        "reads_returns": False,
        "search_started": False,
        "winner_selected": False,
        "candidate_created": False,
        "sealed_validation_read": False,
        "catalog_mutated": False,
        "platform_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "prototype_registry.json", registry)
    _write(artifacts / "prototype_preregistration_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S008 EX19 执行记录\n\n"
        "校验EX18的13个组件身份、三个原型的结构差异，以及用户确认的执行与评价合同。"
        "本实验没有读取收益、运行搜索、选择胜者或创建候选。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX19 结论\n\n"
        "裁决：`PROCEED_TO_PROTOTYPE_IMPLEMENTATION_AND_EXPRESSIBILITY_GATE`。"
        "三套原型及共同执行合同已完成不可变预注册。下一步只允许实现三个原型并检查因果、"
        "增量数据、SRT接口和TXE表达性；通过前不得启动联合搜索。\n",
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

