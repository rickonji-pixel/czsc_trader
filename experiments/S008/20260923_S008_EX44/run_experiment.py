from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX44"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


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
        root_left, root_right = find(left), find(right)
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
    repo, artifacts = experiment.parents[2], experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    prohibited = (
        "reads_new_labels", "prototype_instantiation", "starts_search", "candidate_generation",
        "reads_sealed_validation", "mutates_catalog", "mutates_platform", "mutates_pte",
    )
    if any(protocol.get(key) for key in prohibited):
        raise ValueError("long-cycle component review scope exceeds contract")

    ex43 = repo / "experiments/S008/20260923_S008_EX43"
    ex16 = repo / "experiments/S008/20260923_S008_EX16"
    validate_experiment_archive(ex43)
    validate_experiment_archive(ex16)
    source_paths = {
        "ex43_manifest_sha256": ex43 / "experiment_manifest.json",
        "primary_supported_sha256": ex43 / "artifacts/primary_supported_paths.csv",
        "feature_panel_sha256": ex16 / "artifacts/causal_feature_panel.csv.gz",
    }
    for key, path in source_paths.items():
        if _sha256(path) != protocol["sources"][key]:
            raise ValueError(f"frozen source differs: {path}")

    supported = pd.read_csv(source_paths["primary_supported_sha256"])
    expected = int(protocol["supported_path_count"])
    if len(supported) != expected or supported["feature"].nunique() != expected or not supported["horizon_sessions"].eq(int(protocol["primary_horizon_sessions"])).all():
        raise ValueError("supported long-cycle path identity differs from protocol")
    panel = pd.read_csv(source_paths["feature_panel_sha256"])
    panel["Date"] = pd.to_datetime(panel.pop("Date"), errors="raise")
    panel = panel.set_index("Date").sort_index()
    panel = panel.loc[
        panel.index.to_series().between(protocol["confirmation_start"], protocol["confirmation_end"]),
        supported["feature"].tolist(),
    ]
    orientation = supported.set_index("feature")["orientation"].map({"POSITIVE": 1.0, "NEGATIVE": -1.0})
    if orientation.isna().any():
        raise ValueError("unsupported path orientation")
    correlation = panel.mul(orientation, axis=1).corr(method="spearman")
    if correlation.isna().all(axis=None):
        raise ValueError("long-cycle component correlation matrix is empty")
    correlation.to_csv(artifacts / "confirmation_spearman_correlation.csv", encoding="utf-8")

    components = _components(correlation, float(protocol["absolute_spearman_cluster_threshold"]))
    if sorted(name for group in components for name in group) != sorted(supported["feature"]):
        raise ValueError("redundancy clusters do not cover every supported path")
    evidence_rank = {"DIRECTIONALLY_STABLE": 1, "NOMINAL_SUPPORT": 2, "FDR_SUPPORTED": 3}
    supported = supported.copy()
    supported["evidence_rank"] = supported["evidence_label"].map(evidence_rank)
    by_feature = supported.set_index("feature")
    representatives: list[str] = []
    cluster_rows: list[dict[str, object]] = []
    ledger_rows: list[dict[str, object]] = []
    for ordinal, members in enumerate(components, start=1):
        cluster_id = f"L{ordinal:03d}"
        ranked = by_feature.loc[members].sort_values(
            ["evidence_rank", "global_bh_qvalue", "confirmation_residual_ic", "bootstrap_positive_probability", "path_id"],
            ascending=[False, True, False, False, True],
            kind="stable",
        )
        representative = str(ranked.index[0])
        representatives.append(representative)
        internal = np.abs(correlation.loc[members, members].to_numpy(dtype=float))
        maximum_internal = 0.0 if len(members) == 1 else float(np.nanmax(internal[np.triu_indices(len(members), 1)]))
        cluster_rows.append({
            "cluster_id": cluster_id,
            "member_count": len(members),
            "members": " | ".join(members),
            "representative": representative,
            "maximum_internal_absolute_correlation": maximum_internal,
        })
        for member in members:
            row = by_feature.loc[member]
            ledger_rows.append({
                "cluster_id": cluster_id,
                "feature": member,
                "representative": representative,
                "selected": member == representative,
                "reason": "REPRESENTATIVE" if member == representative else "REDUNDANT_WITH_REPRESENTATIVE",
                "absolute_correlation_with_representative": abs(float(correlation.loc[member, representative])),
                "hypothesis_id": row["hypothesis_id"],
                "information_family": row["information_family"],
                "financial_role": row["financial_role"],
                "evidence_label": row["evidence_label"],
                "global_bh_qvalue": row["global_bh_qvalue"],
                "confirmation_residual_ic": row["confirmation_residual_ic"],
            })

    component_panel = by_feature.loc[representatives].reset_index().sort_values(["hypothesis_id", "information_family", "feature"]).reset_index(drop=True)
    role_counts = Counter(component_panel["financial_role"])
    hypothesis_counts = Counter(component_panel["hypothesis_id"])
    family_counts = Counter(component_panel["information_family"])
    required_roles = set(protocol["required_financial_roles"])
    observed_roles = set(component_panel["financial_role"])
    passes = bool(
        required_roles.issubset(observed_roles)
        and len(hypothesis_counts) >= int(protocol["minimum_hypothesis_count"])
        and len(family_counts) >= int(protocol["minimum_information_family_count"])
    )
    decision = "PROCEED_TO_P05_PREREGISTRATION" if passes else "STOP_LONG_CYCLE_COMPONENT_PANEL_INCOMPLETE"
    pd.DataFrame(cluster_rows).to_csv(artifacts / "redundancy_clusters.csv", index=False, encoding="utf-8", lineterminator="\n")
    pd.DataFrame(ledger_rows).to_csv(artifacts / "redundancy_ledger.csv", index=False, encoding="utf-8", lineterminator="\n")
    component_panel.to_csv(artifacts / "long_cycle_component_panel.csv", index=False, encoding="utf-8", lineterminator="\n")
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "decision": decision,
        "input_path_count": len(supported),
        "cluster_count": len(components),
        "representative_count": len(component_panel),
        "redundant_path_count": len(supported) - len(component_panel),
        "hypothesis_counts": dict(sorted(hypothesis_counts.items())),
        "information_family_counts": dict(sorted(family_counts.items())),
        "financial_role_counts": dict(sorted(role_counts.items())),
        "missing_required_roles": sorted(required_roles - observed_roles),
        "reads_new_labels": False,
        "prototype_instantiated": False,
        "search_started": False,
        "candidate_created": False,
        "catalog_mutated": False,
        "platform_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "long_cycle_component_review.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S008 EX44 执行记录\n\n"
        f"对{len(supported)}条252日支持路径完成开发段相关性聚类，形成{len(components)}个冗余簇和"
        f"{len(component_panel)}项代表组件。未读取新标签、实例化P05或启动搜索。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX44 结论\n\n"
        f"机器裁决：`{decision}`。长期状态组件面板含{len(component_panel)}项低冗余信息，覆盖"
        f"{len(hypothesis_counts)}类经济假设、{len(family_counts)}个信息家族和{len(role_counts)}类金融职责。"
        "组件仅取得P05设计资格，尚未构成策略规则或候选证据。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": protocol["experiment_type"],
        "strategy_id": protocol["strategy_id"],
        "credential_id": protocol["credential_id"],
        "symbol": protocol["symbol"],
        "development_cutoff": protocol["development_cutoff"],
        "decision": decision,
        "promotion_allowed": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
