from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX06"
LABEL_PRIORITY = {"FDR_SUPPORTED": 3, "NOMINAL_SUPPORT": 2, "DIRECTIONALLY_STABLE": 1}


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


class _Clusters:
    def __init__(self, items: list[str]) -> None:
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = ("template_selection", "search_started", "candidate_generation", "promotion_allowed", "mutates_catalog", "mutates_strategy_manager", "mutates_pte")
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX06 cannot instantiate, search, promote, or deploy a strategy")

    sources = protocol["sources"]
    ex04 = repo / str(sources["ex04_archive"])
    ex05 = repo / str(sources["ex05_archive"])
    validate_experiment_archive(ex04)
    validate_experiment_archive(ex05)
    frozen = {
        ex05 / "experiment_manifest.json": sources["ex05_manifest_sha256"],
        ex05 / "artifacts/information_path_ledger.csv.gz": sources["information_ledger_sha256"],
        ex05 / "artifacts/information_audit.json": sources["information_audit_sha256"],
        ex04 / "artifacts/causal_feature_panel.csv.gz": sources["feature_panel_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen source differs: {path}")

    ledger = pd.read_csv(ex05 / "artifacts/information_path_ledger.csv.gz")
    primary = ledger.loc[ledger["horizon_sessions"].eq(int(protocol["primary_horizon_sessions"]))].copy()
    eligible_labels = set(protocol["eligible_evidence_labels"])
    review = primary.loc[primary["evidence_label"].isin(eligible_labels)].copy()
    if review.empty:
        raise ValueError("no primary information path entered role review")
    horizon_positive = (
        ledger.loc[ledger["feature"].isin(review["feature"])]
        .assign(positive=lambda frame: frame["confirmation_ic"].gt(0))
        .groupby("feature")["positive"]
        .sum()
    )
    review["positive_horizons"] = review["feature"].map(horizon_positive).astype(int)
    review["review_status"] = "ELIGIBLE"
    review.loc[
        review["top_minus_bottom_return"].le(float(protocol["tail_spread_minimum_exclusive"])),
        "review_status",
    ] = "TAIL_DIRECTION_CONFLICT"
    review.loc[
        review["review_status"].eq("ELIGIBLE")
        & review["positive_horizons"].lt(int(protocol["minimum_positive_horizons"])),
        "review_status",
    ] = "HORIZON_DIRECTION_FRAGILE"
    roles = protocol["role_by_information_family"]
    review["financial_role"] = review["information_family"].map(roles)
    review.loc[
        review["review_status"].eq("ELIGIBLE") & review["financial_role"].isna(),
        "review_status",
    ] = "ROLE_UNRESOLVED"

    eligible = review.loc[review["review_status"].eq("ELIGIBLE")].copy()
    panel = pd.read_csv(ex04 / "artifacts/causal_feature_panel.csv.gz", parse_dates=["date"]).set_index("date")
    confirmation = panel.loc[panel.index >= pd.Timestamp(protocol["confirmation_start"]), eligible["feature"]]
    correlation = confirmation.corr(method="spearman").abs()
    correlation.to_csv(artifacts / "eligible_absolute_spearman.csv", encoding="utf-8-sig", lineterminator="\n")

    clusters = _Clusters(sorted(eligible["feature"].tolist()))
    threshold = float(protocol["redundancy_absolute_spearman"])
    features = sorted(eligible["feature"].tolist())
    pair_rows: list[dict[str, object]] = []
    for index, left in enumerate(features):
        for right in features[index + 1 :]:
            value = float(correlation.loc[left, right])
            redundant = value >= threshold
            if redundant:
                clusters.union(left, right)
            pair_rows.append({"left": left, "right": right, "absolute_spearman": value, "redundant": redundant})
    pd.DataFrame(pair_rows).to_csv(artifacts / "pairwise_redundancy.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    eligible["cluster_root"] = eligible["feature"].map(clusters.find)
    roots = {root: f"CLUSTER-{ordinal:02d}" for ordinal, root in enumerate(sorted(eligible["cluster_root"].unique()), start=1)}
    eligible["cluster_id"] = eligible["cluster_root"].map(roots)
    eligible["label_priority"] = eligible["evidence_label"].map(LABEL_PRIORITY)
    representatives: list[str] = []
    for _, group in eligible.groupby("cluster_id", sort=True):
        chosen = group.sort_values(
            ["label_priority", "hac_one_sided_pvalue", "confirmation_residual_ic", "feature"],
            ascending=[False, True, False, True],
        ).iloc[0]
        representatives.append(str(chosen["feature"]))
    eligible["cluster_status"] = np.where(eligible["feature"].isin(representatives), "REPRESENTATIVE", "REDUNDANT_ALTERNATE")
    review = review.merge(
        eligible[["feature", "cluster_id", "cluster_status"]],
        on="feature",
        how="left",
        validate="one_to_one",
    )
    review.to_csv(artifacts / "role_review_ledger.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    selected = review.loc[review["cluster_status"].eq("REPRESENTATIVE")].copy()
    selected = selected.sort_values(["financial_role", "information_family", "feature"])
    selected.to_csv(artifacts / "representative_component_panel.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    status_counts = review["review_status"].value_counts().sort_index()
    role_counts = selected["financial_role"].value_counts().sort_index()
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": "PROCEED_TO_STRATEGY_PROTOTYPE_PREREGISTRATION" if len(selected) >= 3 else "STOP_INSUFFICIENT_COMPLEMENTARY_COMPONENTS",
        "primary_paths_reviewed": int(len(review)),
        "review_status_counts": {str(key): int(value) for key, value in status_counts.items()},
        "eligible_before_redundancy": int(len(eligible)),
        "redundancy_clusters": int(eligible["cluster_id"].nunique()),
        "representative_components": int(len(selected)),
        "representative_by_role": {str(key): int(value) for key, value in role_counts.items()},
        "representative_ids": selected["feature"].tolist(),
        "template_selected": False,
        "search_started": False,
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "component_review.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S007 EX06 执行\n\n"
        f"状态：`COMPLETE`。复核主周期{len(review)}条稳定路径，尾部与期限门后保留"
        f"{len(eligible)}条，相关性聚为{eligible['cluster_id'].nunique()}簇并保留{len(selected)}名代表。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX06 结论\n\n"
        f"裁决：`{evidence['decision']}`。最终组件面板含{len(selected)}项低冗余信息，职责覆盖"
        f"{', '.join(f'{key}={value}' for key, value in role_counts.items())}。"
        "被剔除路径及原因均保留在审计总账。下一步必须先预注册有限策略原型，再允许调用STC和Optuna。\n",
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
