from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import time

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S005_EX73"


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


def _ordered(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.sort_values(
        ["hac_one_sided_pvalue", "confirmation_residual_ic", "signal_id"],
        ascending=[True, False, True],
    )


def _cluster(ids: list[str], correlation: pd.DataFrame, threshold: float) -> list[list[str]]:
    parent = {value: value for value in ids}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for index, left in enumerate(ids):
        for right in ids[index + 1 :]:
            value = correlation.loc[left, right]
            if pd.notna(value) and abs(float(value)) >= threshold:
                union(left, right)
    groups: defaultdict[str, list[str]] = defaultdict(list)
    for value in ids:
        groups[find(value)].append(value)
    return [sorted(values) for values in groups.values()]


def main() -> None:
    started = time.perf_counter()
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID or bool(protocol.get("reads_new_returns")):
        raise ValueError("EX73 only permits evidence-preserving architecture design")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX73 cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    source = repo / "experiments" / "S005" / str(sources["information_experiment_id"])
    validate_experiment_archive(source)
    frozen_files = (
        (source / "experiment_manifest.json", sources["manifest_sha256"]),
        (source / "artifacts/signal_information_ledger.csv.gz", sources["signal_ledger_sha256"]),
        (source / "artifacts/confirmation_score_matrix.csv.gz", sources["score_matrix_sha256"]),
    )
    for path, expected in frozen_files:
        if _sha256(path) != str(expected):
            raise ValueError(f"frozen source differs: {path}")

    ledger = pd.read_csv(source / "artifacts/signal_information_ledger.csv.gz")
    scores = pd.read_csv(source / "artifacts/confirmation_score_matrix.csv.gz")
    scores = scores.set_index("date")
    rules = protocol["opportunity_selection"]
    opportunity = ledger.loc[
        ledger["evidence_label"].eq(str(rules["evidence_label"]))
        & ledger["frequency"].eq(str(rules["frequency"]))
    ].copy()
    ids = list(map(str, opportunity["signal_id"]))
    correlation = scores.loc[:, ids].corr(method="spearman")
    clusters = _cluster(ids, correlation, float(rules["absolute_spearman_cluster_threshold"]))
    representative_ids: list[str] = []
    cluster_rows: list[dict[str, object]] = []
    indexed = opportunity.set_index("signal_id")
    for cluster_id, members in enumerate(clusters, start=1):
        ranked = _ordered(indexed.loc[members].reset_index())
        representative = str(ranked.iloc[0]["signal_id"])
        representative_ids.append(representative)
        for member in members:
            cluster_rows.append({
                "cluster_id": cluster_id,
                "signal_id": member,
                "representative_signal_id": representative,
                "is_representative": member == representative,
            })
    representatives = opportunity.loc[opportunity["signal_id"].isin(representative_ids)].copy()
    selected_opportunity = (
        _ordered(representatives)
        .groupby("information_family", as_index=False, sort=True)
        .head(1)
        .copy()
    )
    selected_opportunity["architecture_role"] = "OPPORTUNITY_FAMILY_REPRESENTATIVE"

    environment_rules = protocol["environment_selection"]
    environment = ledger.loc[
        ledger["evidence_label"].eq(str(environment_rules["evidence_label"]))
        & ledger["frequency"].eq(str(environment_rules["frequency"]))
        & ledger["information_family"].eq(str(environment_rules["information_family"]))
    ].copy()
    if environment.empty:
        raise ValueError("no daily volatility environment component satisfies frozen evidence rule")
    selected_environment = _ordered(environment).head(1).copy()
    selected_environment["architecture_role"] = "VOLATILITY_ENVIRONMENT"

    selected = pd.concat([selected_opportunity, selected_environment], ignore_index=True, sort=False)
    selected_columns = [
        "architecture_role", "signal_id", "catalog_signal_id", "name", "frequency",
        "information_family", "evidence_label", "confirmation_ic", "confirmation_residual_ic",
        "hac_one_sided_pvalue", "bh_qvalue", "positive_confirmation_years",
        "nonzero_score_sessions", "rolling_60_state_change_median",
        "rolling_60_state_change_p10", "industry_score_correlation",
        "contains_restored_state", "s001_reference_function",
    ]
    selected.loc[:, selected_columns].to_csv(
        artifacts / "selected_components.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    pd.DataFrame(cluster_rows).sort_values(["cluster_id", "signal_id"]).to_csv(
        artifacts / "opportunity_clusters.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )

    selected_ids = list(map(str, selected["signal_id"]))
    selected_correlation = scores.loc[:, selected_ids].corr(method="spearman")
    selected_correlation.index.name = "signal_id"
    selected_correlation.reset_index().to_csv(
        artifacts / "selected_component_correlations.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    off_diagonal = selected_correlation.where(~pd.DataFrame(
        [[left == right for right in selected_ids] for left in selected_ids],
        index=selected_ids,
        columns=selected_ids,
    ))
    maximum_selected_correlation = float(off_diagonal.abs().max().max())

    architecture = {
        "schema_version": 1,
        "opportunity_core": list(map(str, selected_opportunity["signal_id"])),
        "opportunity_information_families": sorted(map(str, selected_opportunity["information_family"])),
        "volatility_environment": str(selected_environment.iloc[0]["signal_id"]),
        "industry_confidence_modulator": str(sources["industry_component"]),
        "planned_architectures": protocol["planned_architectures"],
        "combination_policy": "equal weight across opportunity families; environment and industry evaluated by fixed ablation",
        "industry_constraint": "position confidence only; must not change signal count",
        "execution_policy": "must be preregistered identically for all four paths in the next experiment",
    }
    _write(artifacts / "architecture_plan.json", architecture)

    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "nominal_30m_opportunity_candidates": int(len(opportunity)),
        "opportunity_clusters": int(len(clusters)),
        "opportunity_representatives_after_clustering": int(len(representatives)),
        "selected_opportunity_components": int(len(selected_opportunity)),
        "selected_opportunity_families": sorted(map(str, selected_opportunity["information_family"])),
        "selected_environment_components": int(len(selected_environment)),
        "maximum_absolute_selected_component_correlation": maximum_selected_correlation,
        "maximum_absolute_selected_industry_correlation": float(selected["industry_score_correlation"].abs().max()),
        "component_frequency_policy": protocol["component_frequency_policy"],
        "complete_strategy_frequency_policy": protocol["complete_strategy_frequency_policy"],
        "reads_new_returns": False,
        "decision": "PROCEED_TO_FOUR_PATH_COMPLETE_STRATEGY_PREREGISTRATION",
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": _sha256(protocol_path),
    }
    _write(artifacts / "architecture_evidence.json", evidence)

    names = "、".join(
        f"{row.information_family}:{row.name}" for row in selected_opportunity.itertuples(index=False)
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX73 执行\n\n"
        f"状态：`COMPLETE`。27个30分钟名义支持配置压缩为{len(clusters)}个相关簇，从"
        f"{len(representatives)}个非冗余代表中按信息族选择{len(selected_opportunity)}项；另选择1项"
        "日线波动环境组件。没有读取新收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX73 结论\n\n"
        "裁决：`PROCEED_TO_FOUR_PATH_COMPLETE_STRATEGY_PREREGISTRATION`。机会核心覆盖："
        + names
        + f"。日线波动环境为`{selected_environment.iloc[0]['name']}`，外部调节固定为行业资金流。"
        f"所选技术组件两两最大绝对相关{maximum_selected_correlation:.3f}，与行业资金流最大绝对相关"
        f"{evidence['maximum_absolute_selected_industry_correlation']:.3f}。\n\n"
        "该架构用四个机会信息族、一个波动环境和一个外部资金流来源覆盖结构、趋势、位置、量价、"
        "风险与行业需求，复杂度适合OPC维护。下一轮必须先统一冻结完整交易规则，再运行四条消融"
        "路径；任何一条通过频率和风险收益门后，才有候选资格。\n",
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
