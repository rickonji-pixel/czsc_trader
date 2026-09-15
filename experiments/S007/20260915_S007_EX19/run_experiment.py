from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from factor_signal_catalog import CatalogRegistry
from strategy_template_catalog import TemplateRegistry

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX19"


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
        raise ValueError("EX19 protocol identity or return-access declaration differs")
    forbidden = ("search_started", "candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX19 cannot search, promote, or deploy a strategy")

    sources = protocol["sources"]
    ex18 = repo / str(sources["ex18_archive"])
    ex12 = repo / str(sources["ex12_archive"])
    validate_experiment_archive(ex18)
    validate_experiment_archive(ex12)
    frozen = {
        ex18 / "experiment_manifest.json": sources["ex18_manifest_sha256"],
        ex18 / "artifacts/identifiability_evidence.json": sources["identifiability_evidence_sha256"],
        ex12 / "experiment_manifest.json": sources["ex12_manifest_sha256"],
        repo / str(sources["ex09_feasible_trials"]): sources["ex09_feasible_trials_sha256"],
        repo / str(sources["effective_search_protocol"]): sources["effective_search_protocol_sha256"],
        repo / "catalog/factors/s007.json": sources["s007_factor_definitions_sha256"],
        repo / "strategy_templates/templates.json": sources["stc_templates_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen gated-prototype source differs: {path}")

    identifiability = _read(ex18 / "artifacts/identifiability_evidence.json")
    supported = identifiability["supported_features"]
    if supported != [{"feature": protocol["supported_pre_entry_feature"], "direction": "HIGHER"}]:
        raise ValueError("EX18 does not support the preregistered confirmation gate")

    fsc = CatalogRegistry(repo / "catalog")
    stc = TemplateRegistry(repo / "strategy_templates")
    if fsc.digest != sources["fsc_digest"] or stc.digest != sources["stc_digest"]:
        raise ValueError("FSC or STC digest differs from frozen protocol")

    ex09 = pd.read_csv(repo / str(sources["ex09_feasible_trials"]))
    anchor_rows = ex09.loc[ex09["trial_id"].eq(protocol["base_anchor_trial_id"])]
    if len(anchor_rows) != 1:
        raise ValueError("base anchor is not unique in EX09 feasible trials")
    metadata = json.loads(anchor_rows.iloc[0]["metadata"])
    component = _read(artifacts / "component_contract.json")
    if metadata["weights"] != component["base_score"]["weights"]:
        raise ValueError("component weights differ from the frozen platform medoid")
    if float(metadata["entry_threshold"]) != float(component["base_score"]["entry_threshold"]):
        raise ValueError("component entry threshold differs from the frozen platform medoid")
    if float(metadata["exit_threshold"]) != float(component["base_score"]["exit_threshold"]):
        raise ValueError("component exit threshold differs from the frozen platform medoid")

    effective = _read(repo / str(sources["effective_search_protocol"]))
    factor_ids = {name: value["factor_id"] for name, value in effective["feature_bindings"].items()}
    if set(component["base_score"]["weights"]) != set(factor_ids):
        raise ValueError("component features differ from the frozen seven-factor definition")
    for factor_id in factor_ids.values():
        if fsc.show(str(factor_id))["kind"] != "factor":
            raise ValueError(f"component source is not a factor: {factor_id}")

    prototype = _read(artifacts / "prototype.json")
    instance = stc.instantiate(str(prototype["template_id"]), prototype["bindings"], prototype["parameters"])
    search = _read(artifacts / "search_protocol.json")
    grid = search["parameters"]["confirmation_gate_quantile"]["values"]
    if len(grid) != 21 or grid[0] != 0.0 or grid[-1] != 0.5:
        raise ValueError("confirmation gate grid differs from preregistration")
    if any(round(float(grid[index + 1]) - float(grid[index]), 12) != 0.025 for index in range(len(grid) - 1)):
        raise ValueError("confirmation gate grid step differs from preregistration")

    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": "REVIEW_GATED_SCORE_PROTOTYPE",
        "template_id": instance.template_id,
        "template_instance_id": instance.instance_id,
        "base_anchor_trial_id": protocol["base_anchor_trial_id"],
        "confirmation_gate_feature": protocol["supported_pre_entry_feature"],
        "confirmation_gate_applies_to": "ENTRY_ONLY",
        "searched_parameter_count": 1,
        "grid_points": len(grid),
        "search_contract_frozen": True,
        "reads_new_returns": False,
        "search_started": False,
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "prototype_evidence.json", evidence)
    _write(artifacts / "template_instance.json", instance.to_dict())
    (experiment / "03_execution.md").write_text(
        "# S007 EX19 执行\n\n"
        f"状态：`COMPLETE`。已将EX12平台中心总分与EX18确认角色贡献绑定到"
        f"`{instance.template_id}`，实例为`{instance.instance_id}`。冻结21点单参数内存网格，"
        "没有读取收益或启动搜索。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX19 结论\n\n"
        "裁决：`REVIEW_GATED_SCORE_PROTOTYPE`。新原型只在原总分入场条件上增加确认角色的独立"
        "入场门，持仓后的退出规则、七因子权重、目标仓位和10bp成本口径全部保持不变。下一轮"
        "只允许搜索确认门分位数，并要求至少连续3个合格网格点形成平台。按照逐轮评审约定，"
        "本轮不启动搜索，需先与用户评审。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": protocol["experiment_type"],
        "strategy_id": protocol["strategy_id"],
        "symbol": protocol["symbol"],
        "development_cutoff": protocol["development_cutoff"],
        "decision": evidence["decision"],
        "promotion_allowed": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
