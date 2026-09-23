from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX18"


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


def _components(correlation: pd.DataFrame, threshold: float) -> list[list[str]]:
    names = sorted(correlation.columns)
    parent = {name: name for name in names}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(left: str, right: str) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left != root_right:
            parent[max(root_left, root_right)] = min(root_left, root_right)

    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            value = correlation.loc[left, right]
            if pd.notna(value) and abs(float(value)) >= threshold:
                union(left, right)
    grouped: dict[str, list[str]] = {}
    for name in names:
        grouped.setdefault(find(name), []).append(name)
    return sorted((sorted(values) for values in grouped.values()), key=lambda values: values[0])


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    prohibited = (
        "reads_new_return_labels",
        "selects_template",
        "starts_search",
        "candidate_generation",
        "reads_sealed_validation",
        "mutates_catalog",
        "mutates_platform",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in prohibited):
        raise ValueError("redundancy review cannot read new labels, search or mutate")

    ex17 = repo / "experiments/S008/20260923_S008_EX17"
    ex16 = repo / "experiments/S008/20260923_S008_EX16"
    validate_experiment_archive(ex17)
    validate_experiment_archive(ex16)
    source_paths = {
        "ex17_manifest_sha256": ex17 / "experiment_manifest.json",
        "primary_supported_sha256": ex17 / "artifacts/primary_supported_paths.csv",
        "feature_panel_sha256": ex16 / "artifacts/causal_feature_panel.csv.gz",
        "feature_catalog_sha256": ex16 / "artifacts/feature_catalog.csv",
    }
    for key, path in source_paths.items():
        if _sha256(path) != protocol["sources"][key]:
            raise ValueError(f"frozen source differs: {path}")

    supported = pd.read_csv(source_paths["primary_supported_sha256"])
    expected = int(protocol["supported_path_count"])
    if (
        len(supported) != expected
        or supported["feature"].nunique() != expected
        or not supported["horizon_sessions"].eq(int(protocol["primary_horizon_sessions"])).all()
    ):
        raise ValueError("supported primary path identity differs from protocol")
    panel = pd.read_csv(source_paths["feature_panel_sha256"])
    panel["Date"] = pd.to_datetime(panel["Date"], errors="raise")
    panel = panel.set_index("Date").sort_index()
    panel = panel.loc[
        panel.index.to_series().between(
            protocol["confirmation_start"], protocol["confirmation_end"]
        ),
        supported["feature"].tolist(),
    ]
    orientation = supported.set_index("feature")["orientation"].map(
        {"POSITIVE": 1.0, "NEGATIVE": -1.0}
    )
    oriented = panel.mul(orientation, axis=1)
    correlation = oriented.corr(method="spearman")
    if correlation.isna().all(axis=None):
        raise ValueError("confirmation correlation matrix is empty")
    correlation.to_csv(artifacts / "confirmation_spearman_correlation.csv", encoding="utf-8")

    components = _components(
        correlation, float(protocol["absolute_spearman_cluster_threshold"])
    )
    if sorted(name for group in components for name in group) != sorted(supported["feature"]):
        raise ValueError("redundancy components do not cover every supported path")
    evidence_rank = {"DIRECTIONALLY_STABLE": 1, "NOMINAL_SUPPORT": 2, "FDR_SUPPORTED": 3}
    supported = supported.copy()
    supported["evidence_rank"] = supported["evidence_label"].map(evidence_rank)
    cluster_rows: list[dict[str, object]] = []
    elimination_rows: list[dict[str, object]] = []
    representatives: list[str] = []
    by_feature = supported.set_index("feature")
    for ordinal, members in enumerate(components, start=1):
        cluster_id = f"C{ordinal:03d}"
        ranked = by_feature.loc[members].sort_values(
            [
                "evidence_rank",
                "global_bh_qvalue",
                "confirmation_residual_ic",
                "bootstrap_positive_probability",
            ],
            ascending=[False, True, False, False],
            kind="stable",
        )
        best_rank = ranked.iloc[0]
        ties = ranked.loc[
            ranked["evidence_rank"].eq(best_rank["evidence_rank"])
            & ranked["global_bh_qvalue"].eq(best_rank["global_bh_qvalue"])
            & ranked["confirmation_residual_ic"].eq(best_rank["confirmation_residual_ic"])
            & ranked["bootstrap_positive_probability"].eq(
                best_rank["bootstrap_positive_probability"]
            )
        ]
        representative = sorted(ties.index)[0]
        representatives.append(representative)
        maximum_internal_correlation = (
            0.0
            if len(members) == 1
            else float(
                np.nanmax(
                    np.abs(correlation.loc[members, members].to_numpy() - np.eye(len(members)))
                )
            )
        )
        cluster_rows.append(
            {
                "cluster_id": cluster_id,
                "member_count": len(members),
                "members": " | ".join(members),
                "representative": representative,
                "maximum_internal_absolute_correlation": maximum_internal_correlation,
            }
        )
        for member in members:
            row = by_feature.loc[member]
            elimination_rows.append(
                {
                    "cluster_id": cluster_id,
                    "feature": member,
                    "representative": representative,
                    "selected": member == representative,
                    "reason": "REPRESENTATIVE" if member == representative else "REDUNDANT_WITH_REPRESENTATIVE",
                    "absolute_correlation_with_representative": abs(
                        float(correlation.loc[member, representative])
                    ),
                    "hypothesis_id": row["hypothesis_id"],
                    "information_family": row["information_family"],
                    "financial_role": row["financial_role"],
                    "evidence_label": row["evidence_label"],
                    "global_bh_qvalue": row["global_bh_qvalue"],
                    "confirmation_residual_ic": row["confirmation_residual_ic"],
                }
            )

    component_panel = by_feature.loc[representatives].reset_index()
    component_panel = component_panel.sort_values(
        ["financial_role", "hypothesis_id", "feature"]
    ).reset_index(drop=True)
    role_counts = Counter(component_panel["financial_role"])
    hypothesis_counts = Counter(component_panel["hypothesis_id"])
    required_roles = set(protocol["required_financial_roles"])
    observed_roles = set(component_panel["financial_role"])
    passes = bool(
        required_roles.issubset(observed_roles)
        and len(hypothesis_counts) >= int(protocol["minimum_hypothesis_count"])
    )
    decision = (
        "PROCEED_TO_STRATEGY_PROTOTYPE_PREREGISTRATION"
        if passes
        else "STOP_COMPONENT_PANEL_INCOMPLETE"
    )
    pd.DataFrame(cluster_rows).to_csv(
        artifacts / "redundancy_clusters.csv", index=False, encoding="utf-8"
    )
    pd.DataFrame(elimination_rows).to_csv(
        artifacts / "redundancy_ledger.csv", index=False, encoding="utf-8"
    )
    component_panel.to_csv(
        artifacts / "component_panel.csv", index=False, encoding="utf-8"
    )
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "decision": decision,
        "input_path_count": len(supported),
        "cluster_count": len(components),
        "representative_count": len(component_panel),
        "redundant_path_count": len(supported) - len(component_panel),
        "hypothesis_counts": dict(sorted(hypothesis_counts.items())),
        "role_counts": dict(sorted(role_counts.items())),
        "required_roles": sorted(required_roles),
        "missing_required_roles": sorted(required_roles - observed_roles),
        "reads_new_return_labels": False,
        "template_selected": False,
        "search_started": False,
        "candidate_created": False,
        "catalog_mutated": False,
        "platform_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "component_review.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S008 EX18 执行记录\n\n"
        f"对{len(supported)}条主周期稳定路径完成确认期相关性聚类，形成{len(components)}个冗余簇和"
        f"{len(component_panel)}项代表组件，淘汰{len(supported) - len(component_panel)}条重复路径。"
        "没有读取新收益标签、选择原型或启动搜索。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX18 结论\n\n"
        f"裁决：`{decision}`。最终组件面板含{len(component_panel)}项低冗余信息，覆盖"
        f"{len(hypothesis_counts)}类假设和职责{dict(sorted(role_counts.items()))}。"
        "下一步必须先预注册有限策略原型及职责组合，再允许联合搜索；组件本身不是策略。\n",
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
            "decision": decision,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
