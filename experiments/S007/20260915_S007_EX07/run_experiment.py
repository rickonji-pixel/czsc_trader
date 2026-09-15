from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from factor_signal_catalog import CatalogRegistry
from strategy_template_catalog import TemplateRegistry

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX07"


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
        raise ValueError("EX07 protocol identity or return-access declaration differs")
    if any(protocol.get(key) for key in ("search_started", "candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX07 cannot search, promote, or deploy a strategy")

    sources = protocol["sources"]
    ex06 = repo / str(sources["ex06_archive"])
    validate_experiment_archive(ex06)
    frozen = {
        ex06 / "experiment_manifest.json": sources["ex06_manifest_sha256"],
        ex06 / "artifacts/representative_component_panel.csv": sources["representative_panel_sha256"],
        repo / "catalog/factors/s007.json": sources["s007_factor_definitions_sha256"],
        repo / "catalog/information_families.json": sources["information_families_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen prototype source differs: {path}")

    fsc = CatalogRegistry(repo / "catalog")
    stc = TemplateRegistry(repo / "strategy_templates")
    if fsc.digest != sources["fsc_digest"] or stc.digest != sources["stc_digest"]:
        raise ValueError("FSC or STC digest differs from frozen protocol")
    panel = pd.read_csv(ex06 / "artifacts/representative_component_panel.csv")
    bindings = protocol["feature_bindings"]
    if set(panel["feature"]) != set(bindings):
        raise ValueError("prototype bindings do not exactly cover EX06 representatives")
    for binding in bindings.values():
        definition = fsc.show(str(binding["factor_id"]))
        if definition["kind"] != "factor":
            raise ValueError(f"prototype source is not a factor: {binding['factor_id']}")

    spec = _read(artifacts / "prototype.json")
    instance = stc.instantiate(str(spec["template_id"]), spec["bindings"], spec["parameters"])
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": "PROCEED_TO_PREREGISTERED_PARAMETER_SEARCH",
        "template_id": instance.template_id,
        "template_instance_id": instance.instance_id,
        "input_count": len(instance.bindings),
        "fsc_digest": fsc.digest,
        "stc_digest": stc.digest,
        "normalization": protocol["normalization"],
        "search_contract_frozen": True,
        "search_started": False,
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "prototype_evidence.json", evidence)
    _write(artifacts / "template_instance.json", instance.to_dict())
    (experiment / "03_execution.md").write_text(
        f"# S007 EX07 执行\n\n状态：`COMPLETE`。FSC七项因子与STC模板绑定校验通过，锚点实例为`{instance.instance_id}`。没有读取收益或启动搜索。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX07 结论\n\n裁决：`PROCEED_TO_PREREGISTERED_PARAMETER_SEARCH`。S007首个原型固定为七项弱信息的低复杂度加权评分结构。模板、输入、方向、因果归一化、搜索范围、执行口径和硬门均已事前冻结。\n",
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
