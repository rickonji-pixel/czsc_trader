from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S006_EX08"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _shift_after_close(series: pd.Series, calendar: pd.DatetimeIndex) -> pd.Series:
    mapped: dict[pd.Timestamp, object] = {}
    for source_date, value in series.dropna().items():
        position = int(calendar.searchsorted(pd.Timestamp(source_date).normalize(), side="right"))
        if position < len(calendar):
            mapped[calendar[position]] = value
    return pd.Series(mapped, dtype="object").reindex(calendar)


def _episode_dates(opportunity: pd.Series) -> pd.DatetimeIndex:
    active = opportunity.fillna(False).astype(bool)
    starts = active & ~active.shift(1, fill_value=False)
    return pd.DatetimeIndex(active.index[starts])


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_new_returns"):
        raise ValueError("EX08 protocol identity or return-access declaration differs")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX08 cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    corrected_experiment = repo / "experiments/S006" / str(sources["corrected_audit_experiment_id"])
    validate_experiment_archive(corrected_experiment)
    source_files = {
        corrected_experiment / "experiment_manifest.json": sources["corrected_audit_manifest_sha256"],
        corrected_experiment / "artifacts/corrected_information_path_ledger.csv.gz": sources["corrected_ledger_sha256"],
        repo / str(sources["state_models_path"]): sources["state_models_sha256"],
        repo / str(sources["primary_states_path"]): sources["primary_states_sha256"],
    }
    for path, expected in source_files.items():
        if _sha256(path) != str(expected):
            raise ValueError(f"frozen source differs: {path}")

    context = RepositoryContext.discover(repo, explicit_root=repo)
    replay = load_replay_data(
        context,
        str(protocol["dataset"]["name"]),
        str(protocol["symbol"]),
        str(protocol["asset_type"]),
        date.fromisoformat(str(protocol["development_cutoff"])),
    )
    if replay.fingerprint != str(protocol["dataset"]["fingerprint"]):
        raise ValueError("research dataset differs from frozen protocol")
    prices = replay.adjusted.daily.copy()
    prices["dt"] = pd.to_datetime(prices["dt"]).dt.normalize()
    prices = prices.set_index("dt").sort_index()
    prices = prices.loc[prices.index <= pd.Timestamp(protocol["development_cutoff"])]
    calendar = prices.index
    opens = prices["open"].astype(float)

    ledger = pd.read_csv(corrected_experiment / "artifacts/corrected_information_path_ledger.csv.gz")
    anchors = ledger.loc[ledger["evidence_label"].eq(str(protocol["scope"]["source_label"]))].copy()
    if len(anchors) != int(protocol["scope"]["expected_paths"]):
        raise ValueError("FDR anchor count differs from protocol")
    models = pd.read_csv(repo / str(sources["state_models_path"]))
    primary = pd.read_csv(repo / str(sources["primary_states_path"]))
    primary["dt"] = pd.to_datetime(primary["dt"]).dt.normalize()
    primary = primary.set_index("dt").reindex(calendar)

    dataset = protocol["dataset"]
    discovery_mask = (calendar >= pd.Timestamp(dataset["discovery_start"])) & (calendar <= pd.Timestamp(dataset["discovery_end"]))
    confirmation_mask = (calendar >= pd.Timestamp(dataset["confirmation_start"])) & (calendar <= pd.Timestamp(dataset["confirmation_end"]))
    criteria = protocol["robust_anchor"]
    score_series: dict[str, pd.Series] = {}
    episode_sets: dict[str, set[pd.Timestamp]] = {}
    rows: list[dict[str, object]] = []

    for anchor in anchors.itertuples(index=False):
        component_id = str(anchor.component_id)
        horizon = int(anchor.horizon_sessions)
        path_id = str(anchor.path_id)
        state_model = models.loc[
            models["component_id"].eq(component_id) & models["horizon_sessions"].eq(horizon)
        ]
        if state_model.empty:
            raise ValueError(f"missing frozen state model: {path_id}")
        mapping = dict(zip(state_model["state"].astype(str), state_model["frozen_score"].astype(float), strict=True))
        states = _shift_after_close(primary[component_id], calendar)
        scores = states.astype(str).map(mapping).where(states.notna()).astype(float)
        score_series[path_id] = scores
        episodes = _episode_dates(scores.gt(0) & scores.notna())
        discovery_episodes = episodes[discovery_mask[calendar.get_indexer(episodes)]]
        confirmation_episodes = episodes[confirmation_mask[calendar.get_indexer(episodes)]]
        episode_sets[path_id] = set(confirmation_episodes)
        outcome = opens.shift(-horizon).div(opens).sub(1.0)
        confirmation_outcome = outcome.loc[confirmation_mask].dropna()
        event_returns = outcome.reindex(confirmation_episodes).dropna()
        baseline = float(confirmation_outcome.mean())
        edge = float(event_returns.mean() - baseline) if len(event_returns) else np.nan
        drop_count = int(criteria["drop_best_episodes"])
        without_best = event_returns.sort_values().iloc[:-drop_count] if len(event_returns) > drop_count else pd.Series(dtype=float)
        edge_without_best = float(without_best.mean() - baseline) if len(without_best) else np.nan
        yearly: dict[str, dict[str, object]] = {}
        positive_years = 0
        eligible_years = 0
        for year, values in event_returns.groupby(event_returns.index.year):
            year_baseline = float(confirmation_outcome.loc[confirmation_outcome.index.year == year].mean())
            year_edge = float(values.mean() - year_baseline)
            eligible = len(values) >= int(criteria["minimum_episodes_per_year"])
            eligible_years += int(eligible)
            positive_years += int(eligible and year_edge > 0)
            yearly[str(int(year))] = {"episodes": int(len(values)), "mean_excess_return": year_edge, "eligible": bool(eligible)}

        enough_samples = (
            len(discovery_episodes) >= int(criteria["minimum_discovery_episodes"])
            and len(event_returns) >= int(criteria["minimum_confirmation_episodes"])
        )
        cross_year = positive_years >= int(criteria["minimum_positive_years"])
        survivor = bool(np.isfinite(edge_without_best) and edge_without_best > 0)
        if enough_samples and cross_year and edge > 0 and survivor:
            label = "ROBUST_ANCHOR"
        elif cross_year and edge > 0:
            label = "LIMITED_SAMPLE"
        else:
            label = "FAIL_ROBUSTNESS"
        rows.append({
            "path_id": path_id,
            "component_id": component_id,
            "catalog_id": str(anchor.catalog_id),
            "information_family": str(anchor.information_family),
            "frequency": str(anchor.frequency),
            "horizon_sessions": horizon,
            "global_bh_qvalue": float(anchor.global_bh_qvalue),
            "confirmation_ic": float(anchor.confirmation_ic),
            "confirmation_residual_ic": float(anchor.confirmation_residual_ic),
            "discovery_episodes": int(len(discovery_episodes)),
            "confirmation_episodes": int(len(event_returns)),
            "eligible_confirmation_years": int(eligible_years),
            "positive_confirmation_years": int(positive_years),
            "confirmation_mean_excess_return": edge,
            "drop_best_two_mean_excess_return": edge_without_best,
            "yearly_evidence_json": json.dumps(yearly, ensure_ascii=False, sort_keys=True),
            "robustness_label": label,
        })

    robustness = pd.DataFrame(rows).sort_values(["robustness_label", "global_bh_qvalue", "path_id"])
    robustness.to_csv(artifacts / "anchor_robustness.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    robust_ids = sorted(robustness.loc[robustness["robustness_label"].eq("ROBUST_ANCHOR"), "path_id"])
    pair_rows: list[dict[str, object]] = []
    for left_index, left in enumerate(robust_ids):
        for right in robust_ids[left_index + 1:]:
            frame = pd.concat([score_series[left].rename("left"), score_series[right].rename("right")], axis=1).loc[confirmation_mask].dropna()
            correlation = float(frame["left"].corr(frame["right"], method="spearman")) if len(frame) >= 3 else np.nan
            union = episode_sets[left] | episode_sets[right]
            jaccard = len(episode_sets[left] & episode_sets[right]) / len(union) if union else np.nan
            redundant = (np.isfinite(correlation) and abs(correlation) >= float(protocol["redundancy"]["absolute_spearman"])) or (
                np.isfinite(jaccard) and jaccard >= float(protocol["redundancy"]["episode_jaccard"])
            )
            pair_rows.append({"left_path_id": left, "right_path_id": right, "confirmation_score_spearman": correlation, "episode_jaccard": jaccard, "redundant": bool(redundant)})
    pairs = pd.DataFrame(pair_rows, columns=["left_path_id", "right_path_id", "confirmation_score_spearman", "episode_jaccard", "redundant"])
    pairs.to_csv(artifacts / "anchor_pairwise_redundancy.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    representatives: list[str] = []
    for path_id in robustness.loc[robustness["robustness_label"].eq("ROBUST_ANCHOR")].sort_values(["global_bh_qvalue", "confirmation_residual_ic"], ascending=[True, False])["path_id"]:
        conflicts = pairs.loc[
            pairs["redundant"] & (
                ((pairs["left_path_id"].eq(path_id)) & pairs["right_path_id"].isin(representatives))
                | ((pairs["right_path_id"].eq(path_id)) & pairs["left_path_id"].isin(representatives))
            )
        ]
        if conflicts.empty:
            representatives.append(str(path_id))
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "audited_paths": int(len(robustness)),
        "search_paths_added": int(protocol["scope"]["additional_search_paths"]),
        "robust_anchor_count": int(robustness["robustness_label"].eq("ROBUST_ANCHOR").sum()),
        "limited_sample_count": int(robustness["robustness_label"].eq("LIMITED_SAMPLE").sum()),
        "failed_robustness_count": int(robustness["robustness_label"].eq("FAIL_ROBUSTNESS").sum()),
        "redundant_pair_count": int(pairs["redundant"].sum()) if len(pairs) else 0,
        "representative_anchor_paths": representatives,
        "decision": "PROCEED_TO_CROSS_TYPE_COMPLEMENTARITY_REVIEW" if representatives else "STOP_NO_ROBUST_ANCHOR",
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "anchor_audit.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S006 EX08 执行\n\n"
        f"状态：`COMPLETE`。审计{len(robustness)}条FDR支持路径，得到{evidence['robust_anchor_count']}条"
        f"稳健锚点、{evidence['limited_sample_count']}条样本受限路径和{evidence['failed_robustness_count']}条"
        f"稳健性失败路径；去冗余后保留{len(representatives)}条代表锚点。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S006 EX08 结论\n\n"
        f"裁决：`{evidence['decision']}`。独立事件体检和去冗余后保留{len(representatives)}条代表锚点。"
        "这些路径只获得进入跨类型互补性研究的资格，尚未形成策略结构或候选。\n",
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
