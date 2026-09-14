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


EXPERIMENT_ID = "20260914_S006_EX09"


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


def _episode_dates(active: pd.Series) -> pd.DatetimeIndex:
    flags = active.fillna(False).astype(bool)
    starts = flags & ~flags.shift(1, fill_value=False)
    return pd.DatetimeIndex(flags.index[starts])


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_new_returns"):
        raise ValueError("EX09 protocol identity or return declaration differs")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX09 cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    predecessor = repo / "experiments/S006" / str(sources["failed_predecessor_experiment_id"])
    corrected = repo / "experiments/S006" / str(sources["corrected_audit_experiment_id"])
    validate_experiment_archive(predecessor)
    validate_experiment_archive(corrected)
    frozen = {
        predecessor / "experiment_manifest.json": sources["failed_predecessor_manifest_sha256"],
        corrected / "experiment_manifest.json": sources["corrected_audit_manifest_sha256"],
        corrected / "artifacts/corrected_information_path_ledger.csv.gz": sources["corrected_ledger_sha256"],
        repo / str(sources["state_models_path"]): sources["state_models_sha256"],
        repo / str(sources["primary_states_path"]): sources["primary_states_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != str(expected):
            raise ValueError(f"frozen source differs: {path}")

    context = RepositoryContext.discover(repo, explicit_root=repo)
    replay = load_replay_data(context, str(protocol["dataset"]["name"]), str(protocol["symbol"]), str(protocol["asset_type"]), date.fromisoformat(str(protocol["development_cutoff"])))
    if replay.fingerprint != str(protocol["dataset"]["fingerprint"]):
        raise ValueError("research dataset differs from protocol")
    prices = replay.adjusted.daily.copy()
    prices["dt"] = pd.to_datetime(prices["dt"]).dt.normalize()
    prices = prices.set_index("dt").sort_index()
    prices = prices.loc[prices.index <= pd.Timestamp(protocol["development_cutoff"])]
    calendar = prices.index
    opens = prices["open"].astype(float)
    dataset = protocol["dataset"]
    discovery_mask = (calendar >= pd.Timestamp(dataset["discovery_start"])) & (calendar <= pd.Timestamp(dataset["discovery_end"]))
    confirmation_mask = (calendar >= pd.Timestamp(dataset["confirmation_start"])) & (calendar <= pd.Timestamp(dataset["confirmation_end"]))

    ledger = pd.read_csv(corrected / "artifacts/corrected_information_path_ledger.csv.gz")
    anchors = ledger.loc[ledger["evidence_label"].eq(str(protocol["scope"]["source_label"]))].copy()
    if len(anchors) != int(protocol["scope"]["expected_paths"]):
        raise ValueError("anchor count differs from protocol")
    models = pd.read_csv(repo / str(sources["state_models_path"]))
    primary = pd.read_csv(repo / str(sources["primary_states_path"]))
    primary["dt"] = pd.to_datetime(primary["dt"]).dt.normalize()
    primary = primary.set_index("dt").reindex(calendar)
    criteria = protocol["robust_anchor"]
    rows: list[dict[str, object]] = []
    role_scores: dict[str, pd.Series] = {}
    event_sets: dict[str, set[pd.Timestamp]] = {}

    for anchor in anchors.itertuples(index=False):
        path_id = str(anchor.path_id)
        component_id = str(anchor.component_id)
        horizon = int(anchor.horizon_sessions)
        state_model = models.loc[(models["component_id"].eq(component_id)) & (models["horizon_sessions"].eq(horizon))].copy()
        if state_model.empty:
            raise ValueError(f"missing state model: {path_id}")
        weighted_center = float(np.average(state_model["frozen_score"].astype(float), weights=state_model["discovery_occurrences"].astype(float)))
        state_model["centered_score"] = state_model["frozen_score"].astype(float) - weighted_center
        maximum = float(state_model["centered_score"].max())
        minimum = float(state_model["centered_score"].min())
        if maximum >= abs(minimum):
            role = "OPPORTUNITY"
            direction = 1.0
            trigger = set(state_model.loc[np.isclose(state_model["centered_score"], maximum), "state"].astype(str))
        else:
            role = "VETO"
            direction = -1.0
            trigger = set(state_model.loc[np.isclose(state_model["centered_score"], minimum), "state"].astype(str))
        mapping = dict(zip(state_model["state"].astype(str), state_model["centered_score"].astype(float), strict=True))
        states = _shift_after_close(primary[component_id], calendar)
        scores = states.astype(str).map(mapping).where(states.notna()).astype(float) * direction
        role_scores[path_id] = scores
        active = states.astype(str).isin(trigger) & states.notna()
        episodes = _episode_dates(active)
        discovery_episodes = episodes[discovery_mask[calendar.get_indexer(episodes)]]
        confirmation_episodes = episodes[confirmation_mask[calendar.get_indexer(episodes)]]
        event_sets[path_id] = set(confirmation_episodes)
        outcome = opens.shift(-horizon).div(opens).sub(1.0)
        confirmation_outcome = outcome.loc[confirmation_mask].dropna()
        returns = outcome.reindex(confirmation_episodes).dropna()
        baseline = float(confirmation_outcome.mean())
        benefits = direction * (returns - baseline)
        mean_benefit = float(benefits.mean()) if len(benefits) else np.nan
        drop_count = int(criteria["drop_best_episodes"])
        without_best = benefits.sort_values().iloc[:-drop_count] if len(benefits) > drop_count else pd.Series(dtype=float)
        survivor = float(without_best.mean()) if len(without_best) else np.nan
        yearly: dict[str, dict[str, object]] = {}
        positive_years = 0
        eligible_years = 0
        for year, values in returns.groupby(returns.index.year):
            year_baseline = float(confirmation_outcome.loc[confirmation_outcome.index.year == year].mean())
            year_benefit = float((direction * (values - year_baseline)).mean())
            eligible = len(values) >= int(criteria["minimum_episodes_per_year"])
            eligible_years += int(eligible)
            positive_years += int(eligible and year_benefit > 0)
            yearly[str(int(year))] = {"episodes": int(len(values)), "mean_role_benefit": year_benefit, "eligible": bool(eligible)}
        enough = len(discovery_episodes) >= int(criteria["minimum_discovery_episodes"]) and len(returns) >= int(criteria["minimum_confirmation_episodes"])
        cross_year = positive_years >= int(criteria["minimum_positive_years"])
        if enough and cross_year and mean_benefit > 0 and survivor > 0:
            label = "ROBUST_ANCHOR"
        elif cross_year and mean_benefit > 0:
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
            "role": role,
            "trigger_states_json": json.dumps(sorted(trigger), ensure_ascii=False),
            "global_bh_qvalue": float(anchor.global_bh_qvalue),
            "confirmation_residual_ic": float(anchor.confirmation_residual_ic),
            "discovery_episodes": int(len(discovery_episodes)),
            "confirmation_episodes": int(len(returns)),
            "eligible_confirmation_years": int(eligible_years),
            "positive_confirmation_years": int(positive_years),
            "confirmation_mean_role_benefit": mean_benefit,
            "drop_best_two_mean_role_benefit": survivor,
            "yearly_evidence_json": json.dumps(yearly, ensure_ascii=False, sort_keys=True),
            "robustness_label": label,
        })

    robustness = pd.DataFrame(rows).sort_values(["robustness_label", "global_bh_qvalue", "path_id"])
    robustness.to_csv(artifacts / "centered_role_robustness.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    robust_ids = sorted(robustness.loc[robustness["robustness_label"].eq("ROBUST_ANCHOR"), "path_id"])
    pair_rows: list[dict[str, object]] = []
    for index, left in enumerate(robust_ids):
        for right in robust_ids[index + 1:]:
            frame = pd.concat([role_scores[left].rename("left"), role_scores[right].rename("right")], axis=1).loc[confirmation_mask].dropna()
            correlation = float(frame["left"].corr(frame["right"], method="spearman")) if len(frame) >= 3 else np.nan
            union = event_sets[left] | event_sets[right]
            jaccard = len(event_sets[left] & event_sets[right]) / len(union) if union else np.nan
            redundant = (np.isfinite(correlation) and abs(correlation) >= float(protocol["redundancy"]["absolute_spearman"])) or (np.isfinite(jaccard) and jaccard >= float(protocol["redundancy"]["episode_jaccard"]))
            pair_rows.append({"left_path_id": left, "right_path_id": right, "confirmation_role_score_spearman": correlation, "event_jaccard": jaccard, "redundant": bool(redundant)})
    pairs = pd.DataFrame(pair_rows, columns=["left_path_id", "right_path_id", "confirmation_role_score_spearman", "event_jaccard", "redundant"])
    pairs.to_csv(artifacts / "centered_role_pairwise_redundancy.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    representatives: list[str] = []
    ordered = robustness.loc[robustness["robustness_label"].eq("ROBUST_ANCHOR")].sort_values(["global_bh_qvalue", "confirmation_residual_ic"], ascending=[True, False])
    for path_id in ordered["path_id"]:
        conflict = pairs.loc[pairs["redundant"] & (((pairs["left_path_id"].eq(path_id)) & pairs["right_path_id"].isin(representatives)) | ((pairs["right_path_id"].eq(path_id)) & pairs["left_path_id"].isin(representatives)))]
        if conflict.empty:
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
    _write(artifacts / "centered_role_audit.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S006 EX09 执行\n\n"
        f"状态：`COMPLETE`。按发现期居中并锁定职责后审计{len(robustness)}条路径，得到"
        f"{evidence['robust_anchor_count']}条稳健锚点、{evidence['limited_sample_count']}条样本受限路径和"
        f"{evidence['failed_robustness_count']}条失败路径；去冗余后保留{len(representatives)}条代表锚点。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S006 EX09 结论\n\n"
        f"裁决：`{evidence['decision']}`。发现期锁定职责、独立事件体检及去冗余后保留"
        f"{len(representatives)}条代表锚点。它们仅获得跨类型互补性研究资格，尚未形成策略或候选。\n",
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
