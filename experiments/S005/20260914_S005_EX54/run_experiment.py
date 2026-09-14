from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import time

import pandas as pd

from factor_signal_catalog import CatalogRegistry
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S005_EX54"


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


def _role(row: pd.Series) -> str:
    if bool(row["opportunity_pool"]):
        return "OPPORTUNITY"
    if bool(row["technical_eligible"]) and str(row["frequency"]) in {"daily", "weekly"}:
        return "ENVIRONMENT"
    return "SUPPORTING"


def _family_table(frame: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for family, group in frame.groupby("information_family", sort=True):
        opportunity = group.loc[group["research_role"].eq("OPPORTUNITY")]
        environment = group.loc[group["research_role"].eq("ENVIRONMENT")]
        supporting = group.loc[group["research_role"].eq("SUPPORTING")]
        rows.append({
            "information_family": family,
            "eligible_states": int(len(group)),
            "opportunity_states": int(len(opportunity)),
            "opportunity_signal_configurations": int(opportunity["signal_id"].nunique()),
            "environment_states": int(len(environment)),
            "environment_signal_configurations": int(environment["signal_id"].nunique()),
            "supporting_states": int(len(supporting)),
            "frequencies": "|".join(sorted(map(str, group["frequency"].unique()))),
        })
    return pd.DataFrame(rows)


def main() -> None:
    started = time.perf_counter()
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    forbidden = (
        "reads_post_signal_prices", "candidate_generation", "parameter_search",
        "promotion_allowed", "mutates_strategy_manager", "mutates_pte",
    )
    if protocol.get("experiment_id") != EXPERIMENT_ID or any(protocol.get(key) for key in forbidden):
        raise ValueError("EX54 protocol only permits return-free semantic reconciliation")

    catalog = CatalogRegistry(repo / "catalog")
    if catalog.digest != protocol["catalog_digest"]:
        raise ValueError("FSC differs from the frozen EX54 protocol")
    materials_path = repo / str(protocol["materials"]["path"])
    if _sha256(materials_path) != protocol["materials"]["sha256"]:
        raise ValueError("S005 materials differ from the frozen EX54 protocol")

    source = repo / "experiments" / "S005" / str(protocol["source"]["experiment_id"])
    validate_experiment_archive(source)
    eligibility_path = source / "artifacts" / "state_eligibility.csv.gz"
    census_path = source / "artifacts" / "census_evidence.json"
    if _sha256(eligibility_path) != protocol["source"]["state_eligibility_sha256"]:
        raise ValueError("EX49 eligibility evidence differs from the frozen protocol")
    if _sha256(census_path) != protocol["source"]["census_evidence_sha256"]:
        raise ValueError("EX49 census evidence differs from the frozen protocol")

    definitions = {item.name: item for item in catalog.signals if item.provider == "czsc"}
    states = pd.read_csv(eligibility_path)
    states = states.loc[states["technical_eligible"].astype(bool)].copy()
    missing = sorted(set(map(str, states["name"])) - set(definitions))
    if missing:
        raise ValueError(f"eligible CZSC functions missing from FSC: {missing[:3]}")
    states["catalog_signal_id"] = states["name"].map(lambda value: definitions[str(value)].signal_id)
    states["information_family"] = states["name"].map(
        lambda value: definitions[str(value)].information_family
    )
    states["catalog_status"] = states["name"].map(lambda value: definitions[str(value)].status.value)
    states["semantic_basis"] = str(protocol["semantic_basis"])
    states["research_role"] = states.apply(_role, axis=1)
    states = states.sort_values(
        ["research_role", "information_family", "frequency", "name", "state_primary"]
    ).reset_index(drop=True)

    ledger_columns = [
        "state_id", "signal_id", "catalog_signal_id", "frequency", "namespace", "name",
        "state_primary", "information_family", "catalog_status", "semantic_basis",
        "research_role", "signal_coverage", "active_ratio", "independent_transitions",
        "coverage_years", "maximum_year_share", "rolling_60_median", "rolling_60_p10",
        "s001_reference_function", "behavior_sha256",
    ]
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    states.loc[:, ledger_columns].to_csv(
        artifacts / "semantic_state_ledger.csv.gz", index=False, encoding="utf-8",
        compression=compression, lineterminator="\n",
    )
    family = _family_table(states)
    family.to_csv(artifacts / "family_role_coverage.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    opportunity = (
        states.loc[states["research_role"].eq("OPPORTUNITY")]
        .groupby("signal_id", as_index=False)
        .agg(
            catalog_signal_id=("catalog_signal_id", "first"),
            name=("name", "first"), frequency=("frequency", "first"),
            information_family=("information_family", "first"),
            catalog_status=("catalog_status", "first"),
            opportunity_states=("state_primary", lambda values: "|".join(sorted(map(str, values)))),
            opportunity_state_count=("state_id", "size"),
            rolling_60_median=("rolling_60_median", "max"),
            rolling_60_p10=("rolling_60_p10", "max"),
            independent_transitions=("independent_transitions", "max"),
            maximum_year_share=("maximum_year_share", "max"),
            s001_reference_function=("s001_reference_function", "first"),
        )
        .sort_values(["information_family", "name", "signal_id"])
    )
    opportunity.to_csv(artifacts / "opportunity_signal_inventory.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    environment = (
        states.loc[states["research_role"].eq("ENVIRONMENT")]
        .groupby("signal_id", as_index=False)
        .agg(
            catalog_signal_id=("catalog_signal_id", "first"),
            name=("name", "first"), frequency=("frequency", "first"),
            information_family=("information_family", "first"),
            catalog_status=("catalog_status", "first"),
            eligible_states=("state_primary", lambda values: "|".join(sorted(map(str, values)))),
            state_count=("state_id", "size"),
            independent_transitions=("independent_transitions", "max"),
            maximum_year_share=("maximum_year_share", "max"),
            s001_reference_function=("s001_reference_function", "first"),
        )
        .sort_values(["information_family", "frequency", "name", "signal_id"])
    )
    environment.to_csv(artifacts / "environment_signal_inventory.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    project_rows = [row for row in catalog.list_definitions() if row["provider"] != "czsc"]
    project_frame = pd.DataFrame(project_rows).sort_values(["information_family", "kind", "id"])
    project_frame.to_csv(artifacts / "project_definition_inventory.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    family_counts = Counter(map(str, opportunity["information_family"]))
    environment_counts = Counter(map(str, environment["information_family"]))
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "catalog_digest": catalog.digest,
        "eligible_state_count": int(len(states)),
        "mapped_state_count": int(states["information_family"].notna().sum()),
        "unmapped_state_count": int(states["information_family"].isna().sum()),
        "other_technical_state_count": int(states["information_family"].eq("OTHER_TECHNICAL").sum()),
        "opportunity_signal_configurations": int(len(opportunity)),
        "opportunity_family_counts": dict(sorted(family_counts.items())),
        "environment_signal_configurations": int(len(environment)),
        "environment_family_counts": dict(sorted(environment_counts.items())),
        "project_definition_count": int(len(project_frame)),
        "project_information_families": sorted(map(str, project_frame["information_family"].unique())),
        "semantic_limit": "family routing only; DISCOVERED does not imply alpha or detailed rule approval",
        "reads_post_signal_prices": False,
        "decision": "PROCEED_TO_FSC_FAMILY_REPRESENTATIVE_SCREEN",
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": _sha256(protocol_path),
    }
    _write(artifacts / "semantic_evidence.json", evidence)

    family_text = "、".join(f"{key} {value}个" for key, value in sorted(family_counts.items()))
    (experiment / "03_execution.md").write_text(
        "# S005 EX54 执行\n\n"
        f"状态：`COMPLETE`。EX49的{len(states)}个技术合格状态全部关联到FSC定义，"
        f"零缺失、零项停留在待拆分技术类。{len(opportunity)}个中频机会信号配置分属"
        f"{len(family_counts)}个信息族；{len(environment)}个日线/周线配置进入环境候选层。"
        "本轮没有读取收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX54 结论\n\n"
        "裁决：`PROCEED_TO_FSC_FAMILY_REPRESENTATIVE_SCREEN`。早期四类粗分已被FSC的可复跑"
        "语义路由替代。68个中频机会配置覆盖：" + family_text + "。这证明S005现有CZSC弹药"
        "并不局限于结构和EMV两个信号，还包含波动风险、相对位置、日历时点等互补观察面。\n\n"
        "日线/周线环境候选共"
        f"{len(environment)}个配置，覆盖{len(environment_counts)}个信息族；频率门不用于淘汰"
        "环境层。项目级增量数据定义也已列入清单，但既有失败机制不会因进入FSC自动恢复"
        "Alpha资格。\n\n"
        "本轮只完成信息族归位。多数CZSC定义仍为`DISCOVERED`，表示函数已发现、因果边界"
        "可控，但具体规则语义尚需在代表选择时核对；不能解读为可交易或存在Alpha。下一轮"
        "按机会、确认、环境三个职责选择少量代表，结合NMI和既有实验结论去冗余，继续在"
        "冻结组合前禁止读取收益。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": str(protocol["experiment_type"]),
            "strategy_id": str(protocol["strategy_id"]),
            "symbol": str(protocol["symbol"]),
            "development_cutoff": str(protocol["development_cutoff"]),
            "decision": str(evidence["decision"]),
            "candidate_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
