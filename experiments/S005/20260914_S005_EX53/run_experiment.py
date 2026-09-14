from __future__ import annotations

from collections import Counter
import csv
import json
from pathlib import Path

from factor_signal_catalog import CatalogRegistry

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S005_EX53"


def _read(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _write(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    forbidden = (
        "reads_post_signal_prices", "candidate_generation", "parameter_search",
        "promotion_allowed", "mutates_strategy_manager", "mutates_pte",
    )
    if protocol.get("experiment_id") != EXPERIMENT_ID or any(protocol.get(key) for key in forbidden):
        raise ValueError("EX53 protocol permits only a return-free catalog census")

    catalog = CatalogRegistry(repo / "catalog")
    if catalog.digest != protocol["catalog_digest"]:
        raise ValueError("FSC changed after EX53 protocol freeze")
    materials = _read(repo / str(protocol["materials_path"]))
    if materials.get("strategy_id") != "S005" or materials.get("symbol") != "588080.SH":
        raise ValueError("S005 materials identity differs")

    rows = catalog.list_definitions()
    with (artifacts / "catalog_inventory.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    family_rows = []
    for family in catalog.families:
        factors = [item for item in catalog.factors if item.information_family == family.family_id]
        signals = [item for item in catalog.signals if item.information_family == family.family_id]
        family_rows.append({
            "family_id": family.family_id,
            "family_name": family.name,
            "factor_count": len(factors),
            "signal_count": len(signals),
            "ready_factor_count": sum(item.status.value == "READY" for item in factors),
            "ready_signal_count": sum(item.status.value == "READY" for item in signals),
            "needs_semantic_review": sum(item.status.value == "DISCOVERED" for item in (*factors, *signals)),
        })
    with (artifacts / "family_coverage.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(family_rows[0]))
        writer.writeheader()
        writer.writerows(family_rows)

    ready = [row for row in rows if row["status"] == "READY"]
    discovered = [row for row in rows if row["status"] == "DISCOVERED"]
    queue = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "materialize_and_census": [row["id"] for row in ready],
        "semantic_review_before_materialization": [row["id"] for row in discovered],
        "rules": [
            "先物化因子和信号状态，不读取后续收益",
            "技术过滤只处理数据质量、覆盖率和状态密度",
            "信息族代表选择必须留下职责与去冗余依据",
            "机制探测前预注册竞争解释与唯一收益路径",
        ],
    }
    _write(artifacts / "s005_research_queue.json", queue)
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "catalog_digest": catalog.digest,
        "family_count": len(catalog.families),
        "factor_count": len(catalog.factors),
        "signal_count": len(catalog.signals),
        "ready_definition_count": len(ready),
        "discovered_definition_count": len(discovered),
        "provider_counts": dict(sorted(Counter(str(row["provider"]) for row in rows).items())),
        "data_binding_count": len(materials["data_bindings"]),
        "reads_post_signal_prices": False,
        "decision": "START_S005_FSC_TECHNICAL_CENSUS",
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
    }
    _write(artifacts / "census_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S005 EX53 执行\n\n"
        f"FSC校验通过，目录哈希`{catalog.digest}`。共{len(catalog.families)}个信息族、"
        f"{len(catalog.factors)}个因子定义、{len(catalog.signals)}个信号定义；"
        f"{len(ready)}项可直接进入技术普查，{len(discovered)}项需先补语义。"
        "本轮没有读取收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX53 结论\n\n"
        "裁决：`START_S005_FSC_TECHNICAL_CENSUS`。S005不再从某一个现成机制单线试错，"
        "而是先基于项目级FSC形成完整弹药面，再在588080.SH内部完成物化、技术过滤、"
        "去冗余和职责互补选择。项目目录只复用定义；标的计算、缓存和证据仍全部留在S005。\n\n"
        "CZSC注册表中的大多数条目仍为`DISCOVERED`，表示信号函数已发现但金融语义尚未"
        "人工复核；这一状态不能被解释为可交易或存在Alpha。下一轮优先完成语义复核与"
        "标的内技术普查，继续禁止读取后续收益。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": str(protocol["experiment_type"]),
            "strategy_id": "S005",
            "symbol": "588080.SH",
            "development_cutoff": "2026-09-02",
            "decision": str(evidence["decision"]),
            "candidate_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
