from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import time

import pandas as pd
from sklearn.metrics import normalized_mutual_info_score

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S005_EX52"


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
    started = time.perf_counter()
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = (
        "reads_post_signal_prices",
        "candidate_generation",
        "parameter_search",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("coverage audit may not read outcomes, select, promote, or deploy")

    source = repo / "experiments" / "S005" / str(protocol["source"]["experiment_id"])
    validate_experiment_archive(source)
    eligibility_path = source / "artifacts" / "state_eligibility.csv.gz"
    primary_path = source / "artifacts" / "primary_states.csv.gz"
    if _sha256(eligibility_path) != protocol["source"]["eligibility_sha256"]:
        raise ValueError("EX49 eligibility input differs from frozen protocol")
    if _sha256(primary_path) != protocol["source"]["primary_states_sha256"]:
        raise ValueError("EX49 primary-state input differs from frozen protocol")

    eligibility = pd.read_csv(eligibility_path)
    opportunity = eligibility.loc[eligibility["opportunity_pool"].astype(bool)].copy()
    inventory = (
        opportunity.groupby("signal_id", as_index=False)
        .agg(
            frequency=("frequency", "first"),
            namespace=("namespace", "first"),
            name=("name", "first"),
            coarse_family=("role_hint", "first"),
            opportunity_states=("state_primary", lambda values: "|".join(sorted(map(str, values)))),
            opportunity_state_count=("state_id", "size"),
            best_rolling_60_median=("rolling_60_median", "max"),
            best_rolling_60_p10=("rolling_60_p10", "max"),
            maximum_year_share=("maximum_year_share", "max"),
        )
        .sort_values(["coarse_family", "name", "signal_id"])
        .reset_index(drop=True)
    )

    primary = pd.read_csv(primary_path, index_col=0)
    signal_ids = inventory["signal_id"].tolist()
    primary = primary.loc[:, signal_ids]
    name_by_id = inventory.set_index("signal_id")["name"].to_dict()
    family_by_id = inventory.set_index("signal_id")["coarse_family"].to_dict()
    threshold = float(protocol["similarity"]["near_redundancy_threshold"])
    pairs: list[dict[str, object]] = []
    for index, left in enumerate(signal_ids):
        for right in signal_ids[index + 1 :]:
            frame = primary.loc[:, [left, right]].dropna()
            score = float(
                normalized_mutual_info_score(
                    frame[left].astype(str), frame[right].astype(str), average_method="arithmetic"
                )
            )
            pairs.append({
                "left_signal_id": left,
                "left_name": name_by_id[left],
                "left_family": family_by_id[left],
                "right_signal_id": right,
                "right_name": name_by_id[right],
                "right_family": family_by_id[right],
                "nmi": score,
                "near_redundant": score >= threshold,
            })
    pair_frame = pd.DataFrame(pairs).sort_values(
        ["nmi", "left_name", "right_name"], ascending=[False, True, True]
    )

    prototype_names = set(map(str, protocol["prototype"].values()))
    prototype = inventory.loc[inventory["name"].isin(prototype_names)].copy()
    if set(prototype["name"]) != prototype_names:
        raise ValueError("prototype signal missing from opportunity inventory")
    family_counts = Counter(map(str, inventory["coarse_family"]))
    near = pair_frame.loc[pair_frame["near_redundant"].astype(bool)]
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "opportunity_states": int(len(opportunity)),
        "opportunity_signal_configurations": int(len(inventory)),
        "coarse_family_counts": dict(sorted(family_counts.items())),
        "frequency_counts": {
            str(key): int(value) for key, value in inventory["frequency"].value_counts().sort_index().items()
        },
        "pair_count": int(len(pair_frame)),
        "near_redundancy_threshold": threshold,
        "near_redundant_pair_count": int(len(near)),
        "prototype_signal_count": int(len(prototype)),
        "prototype_coarse_families": sorted(map(str, prototype["coarse_family"].unique())),
        "prototype_frequencies": sorted(map(str, prototype["frequency"].unique())),
        "s001_information_group_count": len(protocol["s001_completed_structure"]["information_groups"]),
        "s001_frequency_count": len(protocol["s001_completed_structure"]["frequencies"]),
        "s001_has_regime": bool(protocol["s001_completed_structure"]["regime"]),
        "reads_post_signal_prices": False,
        "decision": "EXPAND_INFORMATION_COVERAGE_BEFORE_RETURN_TEST",
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
    }

    inventory.to_csv(artifacts / "signal_inventory.csv", index=False, lineterminator="\n")
    pair_frame.to_csv(artifacts / "pair_similarity.csv", index=False, lineterminator="\n")
    _write(artifacts / "coverage_evidence.json", evidence)

    top_pairs = pair_frame.head(10)
    pair_lines = ["|左信号|右信号|NMI|近似冗余|", "|---|---|---:|---|"]
    for row in top_pairs.itertuples(index=False):
        pair_lines.append(
            f"|`{row.left_name}`|`{row.right_name}`|{row.nmi:.3f}|{'是' if row.near_redundant else '否'}|"
        )
    (experiment / "03_execution.md").write_text(
        "# S005 EX52 执行\n\n"
        f"状态：`COMPLETE`。136个机会状态折叠为{len(inventory)}个信号配置；计算"
        f"{len(pair_frame)}个信号对的NMI，阈值{threshold:.2f}以上共{len(near)}对。"
        "本轮未读取收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX52 结论\n\n"
        "裁决：`EXPAND_INFORMATION_COVERAGE_BEFORE_RETURN_TEST`。EX49的136项是信号状态，"
        f"折叠后只有{len(inventory)}个信号配置；粗分类为"
        + "、".join(f"{key} {value}个" for key, value in sorted(family_counts.items()))
        + "。全部机会信号均来自30分钟周期。\n\n"
        "EX50原型只覆盖结构与量价效率两个粗信息族，且只有30分钟尺度。它可以作为最小机制"
        "探针，但不足以直接对标S001-v2的三类信息、三种时间尺度与ER60 regime完成态。暂停"
        "该原型的收益测试；下一轮先对信号做人工语义分类，并从技术合格但频率较低的日线、周线"
        "状态中寻找环境层。频率门只约束机会与最终闭合交易，不约束regime本身。\n\n"
        "## 状态行为最相近的信号对\n\n"
        + "\n".join(pair_lines)
        + "\n\nNMI只衡量状态序列依赖程度，不证明金融机制相同。`OTHER_TECHNICAL`仍是待人工拆分的"
        "临时标签。本轮没有创建候选，没有修改SM或PTE。\n",
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
