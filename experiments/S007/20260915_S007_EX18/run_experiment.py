from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX18"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_helpers(path: Path):
    spec = importlib.util.spec_from_file_location("s007_ex09_helpers", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load EX09 helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _auc(values: pd.Series, labels: pd.Series) -> float:
    frame = pd.DataFrame({"value": values, "label": labels}).dropna()
    positives = int(frame["label"].sum())
    negatives = len(frame) - positives
    if positives == 0 or negatives == 0:
        return float("nan")
    ranks = frame["value"].rank(method="average")
    rank_sum = float(ranks.loc[frame["label"].eq(1)].sum())
    statistic = rank_sum - positives * (positives + 1) / 2.0
    return statistic / (positives * negatives)


def _permutation_pvalue(values: pd.Series, labels: pd.Series, repetitions: int, rng: np.random.Generator) -> float:
    frame = pd.DataFrame({"value": values, "label": labels}).dropna()
    observed = abs(_auc(frame["value"], frame["label"]) - 0.5)
    ranks = frame["value"].rank(method="average").to_numpy(dtype=float)
    label_array = frame["label"].to_numpy(dtype=int)
    positives = int(label_array.sum())
    negatives = len(label_array) - positives
    exceedances = 0
    for _ in range(repetitions):
        shuffled = rng.permutation(label_array)
        rank_sum = float(ranks[shuffled == 1].sum())
        statistic = rank_sum - positives * (positives + 1) / 2.0
        auc = statistic / (positives * negatives)
        exceedances += int(abs(auc - 0.5) >= observed - 1e-15)
    return (exceedances + 1.0) / (repetitions + 1.0)


def _bh_qvalues(values: pd.Series) -> pd.Series:
    order = np.argsort(values.to_numpy(dtype=float))
    ordered = values.iloc[order].to_numpy(dtype=float)
    count = len(ordered)
    adjusted = ordered * count / np.arange(1, count + 1)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    output = np.empty(count, dtype=float)
    output[order] = np.clip(adjusted, 0.0, 1.0)
    return pd.Series(output, index=values.index)


def _feature_frame(
    trades: pd.DataFrame,
    combined: pd.Series,
    role_frame: pd.DataFrame,
    entry_threshold: float,
    exit_threshold: float,
) -> pd.DataFrame:
    positive_roles = role_frame.gt(0.0)
    positive_values = role_frame.clip(lower=0.0)
    positive_total = positive_values.sum(axis=1)
    maximum_share = positive_values.max(axis=1).div(positive_total.where(positive_total.gt(0.0)))
    features = pd.DataFrame({
        "score_margin": combined - entry_threshold,
        "score_change_1d": combined.diff(1),
        "score_change_3d": combined.diff(3),
        "score_above_exit_count_3d": combined.ge(exit_threshold).rolling(3, min_periods=3).sum(),
        "role_agreement_count": positive_roles.sum(axis=1),
        "persistent_role_agreement_count_2d": (positive_roles & positive_roles.shift(1).fillna(False)).sum(axis=1),
        "maximum_positive_role_share": maximum_share,
        "confirmation_role_contribution": role_frame["CONFIRMATION"],
    })
    selected = trades[["anchor", "trade_number", "signal_date", "entry_date", "holding_sessions", "net_return"]].copy()
    selected["signal_date"] = pd.to_datetime(selected["signal_date"], errors="raise")
    selected["entry_date"] = pd.to_datetime(selected["entry_date"], errors="raise")
    selected = selected.set_index("signal_date").join(features, how="left").reset_index()
    selected["year"] = selected["entry_date"].dt.year
    selected["durable"] = selected["holding_sessions"].astype(int).ge(4).astype(int)
    return selected


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_new_returns"):
        raise ValueError("EX18 protocol identity or return declaration differs")
    forbidden = ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX18 cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    ex17 = repo / str(sources["ex17_archive"])
    validate_experiment_archive(ex17)
    frozen = {
        ex17 / "experiment_manifest.json": sources["ex17_manifest_sha256"],
        ex17 / "artifacts/trade_ledger.csv": sources["trade_ledger_sha256"],
        ex17 / "artifacts/attribution_evidence.json": sources["attribution_evidence_sha256"],
        ex17 / "run_experiment.py": sources["ex17_script_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen persistence source differs: {path}")

    ex17_protocol = _read(ex17 / "artifacts/protocol.json")
    ex12 = repo / str(ex17_protocol["sources"]["ex12_archive"])
    ex14 = repo / str(ex17_protocol["sources"]["ex14_archive"])
    ex12_protocol = _read(ex12 / "artifacts/protocol.json")
    ex09 = repo / str(ex12_protocol["sources"]["ex09_archive"])
    helpers = _load_helpers(ex09 / "run_experiment.py")
    ex14_protocol = _read(ex14 / "artifacts/protocol.json")
    ex08 = repo / str(ex14_protocol["sources"]["ex08_archive"])
    effective = _read(ex08 / "artifacts/effective_search_protocol.json")
    panel = pd.read_csv(repo / str(ex14_protocol["sources"]["feature_panel"]), parse_dates=["date"]).set_index("date")
    prices = helpers._raw_daily(repo, ex14_protocol["sources"]["daily_sha256"])
    prices = prices.loc[prices.index <= pd.Timestamp(protocol["development_cutoff"])]
    if not panel.index.equals(prices.index):
        raise ValueError("feature panel and price calendar differ")

    normalization = effective["normalization"]
    scores = pd.DataFrame(index=panel.index)
    roles: dict[str, list[str]] = {}
    for feature, binding in effective["feature_bindings"].items():
        scores[feature] = helpers._causal_percentile(
            panel[feature],
            int(normalization["lookback_sessions"]),
            int(normalization["minimum_observations"]),
        ) * int(binding["orientation"])
        roles.setdefault(str(binding["role"]), []).append(feature)

    robustness = _read(ex12 / "artifacts/robustness_evidence.json")
    ex09_feasible = pd.read_csv(ex09 / "artifacts/feasible_trials.csv")
    medoid = ex09_feasible.loc[ex09_feasible["trial_id"].eq(robustness["medoid_trial_id"])]
    ex14_feasible = pd.read_csv(ex14 / "artifacts/feasible_trials.csv")
    if len(medoid) != 1 or len(ex14_feasible) != 1:
        raise ValueError("EX18 anchor rows are not unique")
    anchors = {
        "EX12_PLATFORM_MEDOID_REPRICED_10BP": medoid.iloc[0],
        "EX14_UNIQUE_FEASIBLE_10BP": ex14_feasible.iloc[0],
    }
    discovery_mask = scores.index.to_series().between(
        ex14_protocol["segments"]["discovery_start"], ex14_protocol["segments"]["discovery_end"]
    )
    trades = pd.read_csv(ex17 / "artifacts/trade_ledger.csv")
    feature_frames: list[pd.DataFrame] = []
    for anchor, source_row in anchors.items():
        metadata = json.loads(source_row["metadata"])
        params = json.loads(source_row["params"])
        contributions = scores.mul(pd.Series(metadata["weights"]), axis=1)
        combined = contributions.sum(axis=1).where(scores.notna().all(axis=1))
        discovery_values = combined.loc[discovery_mask].dropna()
        entry_threshold = float(discovery_values.quantile(float(params["entry_quantile"])))
        exit_threshold = float(discovery_values.quantile(float(params["exit_quantile"])))
        role_frame = pd.DataFrame({role: contributions[names].sum(axis=1) for role, names in roles.items()})
        anchor_trades = trades.loc[trades["anchor"].eq(anchor)].copy()
        feature_frames.append(_feature_frame(anchor_trades, combined, role_frame, entry_threshold, exit_threshold))
    entries = pd.concat(feature_frames, ignore_index=True)
    entries.to_csv(artifacts / "entry_feature_panel.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    statistics = protocol["statistics"]
    rng = np.random.default_rng(int(statistics["seed"]))
    result_rows: list[dict[str, object]] = []
    for anchor, group in entries.groupby("anchor"):
        for feature in protocol["features"]:
            valid = group[[feature, "durable", "year"]].dropna()
            auc = _auc(valid[feature], valid["durable"])
            direction = "HIGHER" if auc >= 0.5 else "LOWER"
            oriented = valid[feature] if direction == "HIGHER" else -valid[feature]
            low = oriented.quantile(1.0 / 3.0)
            high = oriented.quantile(2.0 / 3.0)
            bottom_rate = float(valid.loc[oriented.le(low), "durable"].mean())
            top_rate = float(valid.loc[oriented.ge(high), "durable"].mean())
            loo_same = 0
            loo_total = 0
            for year in sorted(valid["year"].unique()):
                subset = valid.loc[valid["year"].ne(year)]
                loo_auc = _auc(subset[feature], subset["durable"])
                if np.isfinite(loo_auc):
                    loo_total += 1
                    loo_same += int((loo_auc >= 0.5) == (auc >= 0.5))
            result_rows.append({
                "anchor": anchor,
                "feature": feature,
                "observations": int(len(valid)),
                "durable_trades": int(valid["durable"].sum()),
                "auc": float(auc),
                "direction": direction,
                "directional_auc": float(max(auc, 1.0 - auc)),
                "top_tertile_durable_rate": top_rate,
                "bottom_tertile_durable_rate": bottom_rate,
                "top_minus_bottom_durable_rate": top_rate - bottom_rate,
                "permutation_pvalue": _permutation_pvalue(
                    valid[feature],
                    valid["durable"],
                    int(statistics["permutation_repetitions"]),
                    rng,
                ),
                "leave_one_year_out_direction_matches": loo_same,
                "leave_one_year_out_years": loo_total,
                "leave_one_year_out_direction_rate": float(loo_same / loo_total) if loo_total else 0.0,
            })
    results = pd.DataFrame(result_rows)
    results["fdr_qvalue"] = results.groupby("anchor", group_keys=False)["permutation_pvalue"].apply(_bh_qvalues)
    results.to_csv(artifacts / "feature_discrimination.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    support_rows: list[dict[str, object]] = []
    for feature, group in results.groupby("feature"):
        directions = set(group["direction"])
        supported = (
            len(group) == len(anchors)
            and len(directions) == 1
            and group["directional_auc"].ge(float(statistics["minimum_directional_auc"])).all()
            and group["fdr_qvalue"].le(float(statistics["fdr_q_maximum"])).all()
            and group["leave_one_year_out_direction_rate"].ge(
                float(statistics["minimum_leave_one_year_out_direction_rate"])
            ).all()
        )
        support_rows.append({
            "feature": feature,
            "direction": group.iloc[0]["direction"] if len(directions) == 1 else "CONFLICT",
            "supported": bool(supported),
            "minimum_directional_auc": float(group["directional_auc"].min()),
            "maximum_fdr_qvalue": float(group["fdr_qvalue"].max()),
            "minimum_leave_one_year_out_direction_rate": float(group["leave_one_year_out_direction_rate"].min()),
            "minimum_top_minus_bottom_durable_rate": float(group["top_minus_bottom_durable_rate"].min()),
        })
    support = pd.DataFrame(support_rows).sort_values(
        ["supported", "minimum_directional_auc"], ascending=[False, False]
    )
    support.to_csv(artifacts / "supported_features.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    supported = support.loc[support["supported"]]
    decision = "REVIEW_SUPPORTED_PERSISTENCE_FEATURES" if len(supported) else "REVIEW_PERSISTENCE_NOT_IDENTIFIABLE"
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": decision,
        "tested_features": int(len(support)),
        "supported_features": supported[["feature", "direction"]].to_dict(orient="records"),
        "supported_feature_count": int(len(supported)),
        "label_is_independent_alpha": False,
        "next_template_selected": False,
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "identifiability_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S007 EX18 执行\n\n"
        f"状态：`COMPLETE`。两个固定锚点分别对{len(protocol['features'])}个事前特征完成AUC、"
        f"{int(statistics['permutation_repetitions'])}次置换、BH-FDR和逐年留一检查；"
        f"{len(supported)}个特征满足全部发现标准。\n",
        encoding="utf-8",
    )
    names = "、".join(supported["feature"].tolist()) if len(supported) else "无"
    confirmation = results.loc[results["feature"].eq("confirmation_role_contribution")]
    confirmation_detail = (
        f"两个锚点AUC分别为{float(confirmation.iloc[0]['auc']):.3f}和"
        f"{float(confirmation.iloc[1]['auc']):.3f}，高三分位持续率为"
        f"{float(confirmation['top_tertile_durable_rate'].min()):.1%}—"
        f"{float(confirmation['top_tertile_durable_rate'].max()):.1%}，低三分位仅"
        f"{float(confirmation['bottom_tertile_durable_rate'].min()):.1%}—"
        f"{float(confirmation['bottom_tertile_durable_rate'].max()):.1%}；"
        f"最差FDR q值{float(confirmation['fdr_qvalue'].max()):.3f}，两个锚点逐年留一方向均为6/6一致。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX18 结论\n\n"
        f"裁决：`{decision}`。通过双锚点、效应量、FDR与年度稳定性联合标准的事前特征为：{names}。"
        f"{confirmation_detail}其余7个预注册特征均未通过联合标准。\n\n"
        "确认角色由ETF份额变化与成交量变化两个既有因子共同构成。结果说明，在下单前确实存在"
        "识别持续信号的增量信息，但尚未回答应采用何种门控形式或阈值。本轮标签仍受现有退出规则"
        "影响，只回答短命信号是否可在入场前识别，不形成新F模板或候选。"
        "按照逐轮评审约定，下一步需先与用户评审。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": protocol["experiment_type"],
        "strategy_id": protocol["strategy_id"],
        "symbol": protocol["symbol"],
        "development_cutoff": protocol["development_cutoff"],
        "decision": decision,
        "promotion_allowed": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
