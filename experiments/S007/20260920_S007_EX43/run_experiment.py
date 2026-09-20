from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import balanced_accuracy_score, roc_auc_score
from sklearn.preprocessing import StandardScaler

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260920_S007_EX43"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_hash(path: Path, expected: str) -> None:
    observed = _sha256(path)
    if observed != expected:
        raise ValueError(f"source hash differs: {path}: {observed} != {expected}")


def _causal_percentile(values: pd.Series, window: int, minimum: int) -> pd.Series:
    def rank_last(items: np.ndarray) -> float:
        current = items[-1]
        valid = items[np.isfinite(items)]
        if not np.isfinite(current) or len(valid) < minimum:
            return np.nan
        return float(
            (np.count_nonzero(valid < current) + 0.5 * np.count_nonzero(valid == current))
            / len(valid)
            - 0.5
        )

    return values.astype(float).rolling(window, min_periods=minimum).apply(
        rank_last, raw=True
    )


def _rank_auc(score: pd.Series, outcome: pd.Series) -> float | None:
    valid = score.notna() & outcome.notna()
    positive = score.loc[valid & outcome.astype(bool)].astype(float).to_numpy()
    negative = score.loc[valid & ~outcome.astype(bool)].astype(float).to_numpy()
    if not len(positive) or not len(negative):
        return None
    comparisons = positive[:, None] - negative[None, :]
    return float(
        (np.count_nonzero(comparisons > 0) + 0.5 * np.count_nonzero(comparisons == 0))
        / comparisons.size
    )


def _permutation_difference(
    values: np.ndarray,
    labels: np.ndarray,
    *,
    trials: int,
    rng: np.random.Generator,
) -> tuple[float, float]:
    observed = float(values[labels].mean() - values[~labels].mean())
    exceedances = 0
    for _ in range(trials):
        shuffled = rng.permutation(labels)
        difference = float(values[shuffled].mean() - values[~shuffled].mean())
        exceedances += abs(difference) >= abs(observed)
    return observed, float((exceedances + 1) / (trials + 1))


def _bh_qvalues(pvalues: pd.Series) -> pd.Series:
    valid = pvalues.dropna().astype(float)
    if valid.empty:
        return pd.Series(np.nan, index=pvalues.index, dtype=float)
    ordered = valid.sort_values()
    count = len(ordered)
    adjusted = ordered.to_numpy() * count / np.arange(1, count + 1)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1].clip(0.0, 1.0)
    result = pd.Series(np.nan, index=pvalues.index, dtype=float)
    result.loc[ordered.index] = adjusted
    return result


def _leave_one_year_out(
    features: pd.DataFrame,
    labels: np.ndarray,
    years: np.ndarray,
    *,
    c_value: float,
    random_seed: int,
) -> tuple[np.ndarray, list[dict[str, object]]]:
    probabilities = np.full(len(features), np.nan, dtype=float)
    fold_rows: list[dict[str, object]] = []
    values = features.to_numpy(dtype=float)
    for year in sorted(set(years)):
        test = years == year
        train = ~test
        if len(set(labels[train])) != 2:
            raise ValueError(f"training fold has one class: {year}")
        scaler = StandardScaler().fit(values[train])
        model = LogisticRegression(
            C=c_value,
            solver="liblinear",
            class_weight="balanced",
            l1_ratio=0,
            random_state=random_seed,
        ).fit(scaler.transform(values[train]), labels[train].astype(int))
        probabilities[test] = model.predict_proba(scaler.transform(values[test]))[:, 1]
        fold_rows.append(
            {
                "held_out_year": int(year),
                "train_count": int(train.sum()),
                "test_count": int(test.sum()),
                "test_hold_better_count": int(labels[test].sum()),
                "test_mean_probability": float(probabilities[test].mean()),
            }
        )
    if not np.isfinite(probabilities).all():
        raise ValueError("cross-year model did not predict every trade")
    return probabilities, fold_rows


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    protocol = _read_json(experiment / "02_protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("EX43 protocol identity differs")
    forbidden = (
        "reads_forward_data",
        "simulates_new_policy",
        "searches_parameters",
        "creates_candidate",
        "changes_strategy_parameters",
        "mutates_pte",
    )
    if any(protocol.get(field) for field in forbidden):
        raise ValueError("EX43 must remain a mechanism diagnostic")

    sources = protocol["sources"]
    counterfactual = repo / str(sources["counterfactual_archive"])
    path_archive = repo / str(sources["path_archive"])
    feature_archive = repo / str(sources["feature_archive"])
    for archive, field in (
        (counterfactual, "counterfactual_manifest_sha256"),
        (path_archive, "path_manifest_sha256"),
        (feature_archive, "feature_manifest_sha256"),
    ):
        validate_experiment_archive(archive)
        _require_hash(archive / "experiment_manifest.json", str(sources[field]))
    _require_hash(
        counterfactual / "artifacts/trade_comparison.csv",
        str(sources["trade_comparison_sha256"]),
    )
    _require_hash(
        counterfactual / "artifacts/overlay_triggers.csv",
        str(sources["overlay_triggers_sha256"]),
    )
    _require_hash(
        counterfactual / "artifacts/counterfactual_evidence.json",
        str(sources["counterfactual_evidence_sha256"]),
    )
    _require_hash(
        path_archive / "artifacts/trade_path_attribution.csv",
        str(sources["trade_path_attribution_sha256"]),
    )
    feature_path = feature_archive / "artifacts/causal_feature_panel.csv.gz"
    _require_hash(feature_path, str(sources["feature_panel_sha256"]))
    version_path = repo / str(sources["strategy_version"])
    _require_hash(version_path, str(sources["strategy_version_sha256"]))

    comparison = pd.read_csv(
        counterfactual / "artifacts/trade_comparison.csv",
        parse_dates=["entry_date", "baseline_exit_date", "overlay_exit_date"],
    )
    comparison = comparison.loc[comparison["overlay_triggered"]].copy()
    triggers = pd.read_csv(
        counterfactual / "artifacts/overlay_triggers.csv",
        parse_dates=["entry_date", "baseline_exit_date", "overlay_exit_date"],
    )
    triggers = triggers.loc[triggers["overlay_triggered"]].copy()
    path = pd.read_csv(
        path_archive / "artifacts/trade_path_attribution.csv",
        parse_dates=["entry_date", "exit_date"],
    )
    if len(comparison) != len(triggers) or len(comparison) != 43:
        raise ValueError("EX43 requires the fixed 43 EX42 triggers")
    if comparison["entry_date"].duplicated().any():
        raise ValueError("EX42 trigger entry dates must be unique")

    rows = comparison.merge(
        triggers[
            [
                "entry_date",
                "entry_price",
                "checkpoint_close",
                "cost_adjusted_mark_to_market_net",
            ]
        ],
        on="entry_date",
        how="left",
        validate="one_to_one",
    )
    path_columns = [
        "entry_date",
        "entry_factor_score",
        "entry_confirmation_score",
        "close_return_s1",
        "factor_change_s1",
        "confirmation_change_s1",
    ]
    rows = rows.merge(
        path[path_columns], on="entry_date", how="left", validate="one_to_one"
    )
    rows["factor_score_s1"] = rows["entry_factor_score"] + rows["factor_change_s1"]
    rows["confirmation_score_s1"] = (
        rows["entry_confirmation_score"] + rows["confirmation_change_s1"]
    )
    rows["hold_advantage"] = rows["baseline_net_return"] - rows["overlay_net_return"]
    rows["hold_better"] = rows["hold_advantage"].gt(0.0)
    rows["action_outcome"] = np.where(rows["hold_better"], "HOLD_BETTER", "EXIT_BETTER")
    rows["entry_year"] = rows["entry_date"].dt.year.astype(int)
    if int(rows["hold_better"].sum()) != 30:
        raise ValueError("EX43 action outcome differs from EX42")

    version = _read_json(version_path)
    rule = version["strategy_payload"]["rule"]
    normalization = rule["normalization"]
    score = rule["score"]
    orientations = {name: int(value) for name, value in score["orientations"].items()}
    panel = pd.read_csv(feature_path, parse_dates=["date"]).set_index("date").sort_index()
    normalized = pd.DataFrame(index=panel.index)
    for feature, orientation in orientations.items():
        normalized[f"norm_{feature}"] = _causal_percentile(
            panel[feature],
            int(normalization["lookback_sessions"]),
            int(normalization["minimum_observations"]),
        ) * orientation
    normalized_at_checkpoint = normalized.reindex(rows["entry_date"]).reset_index(drop=True)
    rows = pd.concat([rows.reset_index(drop=True), normalized_at_checkpoint], axis=1)
    if rows[list(normalized.columns)].isna().any().any():
        raise ValueError("checkpoint normalized factors are incomplete")

    base_rebuilt = rows[[f"norm_{name}" for name in score["base_weights"]]].mul(
        pd.Series(
            {f"norm_{name}": float(weight) for name, weight in score["base_weights"].items()}
        )
    ).sum(axis=1)
    confirmation_rebuilt = rows[
        [f"norm_{name}" for name in score["confirmation_weights"]]
    ].mul(
        pd.Series(
            {
                f"norm_{name}": float(weight)
                for name, weight in score["confirmation_weights"].items()
            }
        )
    ).sum(axis=1)
    if not np.allclose(base_rebuilt, rows["factor_score_s1"], rtol=0.0, atol=1e-12):
        raise ValueError("rebuilt checkpoint base score differs from frozen decision")
    if not np.allclose(
        confirmation_rebuilt,
        rows["confirmation_score_s1"],
        rtol=0.0,
        atol=1e-12,
    ):
        raise ValueError("rebuilt checkpoint confirmation score differs from frozen decision")

    factor_features = [f"norm_{name}" for name in orientations]
    predictors = [
        "cost_adjusted_mark_to_market_net",
        "entry_factor_score",
        "factor_score_s1",
        "factor_change_s1",
        "entry_confirmation_score",
        "confirmation_score_s1",
        "confirmation_change_s1",
        *factor_features,
    ]
    labels = rows["hold_better"].to_numpy(dtype=bool)
    rng = np.random.default_rng(int(protocol["random_seed"]))
    contrast_rows: list[dict[str, object]] = []
    for name in predictors:
        values = rows[name].to_numpy(dtype=float)
        difference, pvalue = _permutation_difference(
            values,
            labels,
            trials=int(protocol["permutation_trials"]),
            rng=rng,
        )
        auc = _rank_auc(rows[name], rows["hold_better"])
        hold_group = rows.loc[rows["hold_better"], name].astype(float)
        exit_group = rows.loc[~rows["hold_better"], name].astype(float)
        contrast_rows.append(
            {
                "feature": name,
                "hold_better_count": int(len(hold_group)),
                "exit_better_count": int(len(exit_group)),
                "hold_better_mean": float(hold_group.mean()),
                "exit_better_mean": float(exit_group.mean()),
                "mean_difference_hold_minus_exit": difference,
                "hold_better_median": float(hold_group.median()),
                "exit_better_median": float(exit_group.median()),
                "auc_hold_better": auc,
                "discrimination_auc": None if auc is None else max(auc, 1.0 - auc),
                "direction": (
                    "HIGHER_IN_HOLD_BETTER" if difference > 0 else "LOWER_IN_HOLD_BETTER"
                ),
                "permutation_pvalue": pvalue,
            }
        )
    contrasts = pd.DataFrame(contrast_rows)
    contrasts["bh_fdr_qvalue"] = _bh_qvalues(contrasts["permutation_pvalue"])
    contrasts = contrasts.sort_values(
        ["bh_fdr_qvalue", "permutation_pvalue", "feature"]
    ).reset_index(drop=True)

    model_features = [
        "cost_adjusted_mark_to_market_net",
        "factor_change_s1",
        "confirmation_change_s1",
        *factor_features,
    ]
    model_spec = protocol["model"]
    years = rows["entry_year"].to_numpy(dtype=int)
    probabilities, fold_rows = _leave_one_year_out(
        rows[model_features],
        labels,
        years,
        c_value=float(model_spec["c"]),
        random_seed=int(protocol["random_seed"]),
    )
    model_auc = float(roc_auc_score(labels.astype(int), probabilities))
    model_balanced_accuracy = float(
        balanced_accuracy_score(labels.astype(int), probabilities >= 0.5)
    )
    model_rng = np.random.default_rng(int(protocol["random_seed"]) + 43)
    model_exceedances = 0
    completed = 0
    for _ in range(int(protocol["model_permutation_trials"])):
        shuffled = model_rng.permutation(labels)
        try:
            permuted_probabilities, _ = _leave_one_year_out(
                rows[model_features],
                shuffled,
                years,
                c_value=float(model_spec["c"]),
                random_seed=int(protocol["random_seed"]),
            )
        except ValueError:
            continue
        permuted_auc = float(
            roc_auc_score(shuffled.astype(int), permuted_probabilities)
        )
        model_exceedances += abs(permuted_auc - 0.5) >= abs(model_auc - 0.5)
        completed += 1
    if completed < int(protocol["model_permutation_trials"]) * 0.95:
        raise ValueError("too many invalid model permutation folds")
    model_pvalue = float((model_exceedances + 1) / (completed + 1))
    rows["cross_year_hold_probability"] = probabilities
    rows["cross_year_predicted_action"] = np.where(
        probabilities >= 0.5, "HOLD_BETTER", "EXIT_BETTER"
    )

    significant = contrasts.loc[contrasts["bh_fdr_qvalue"].le(0.05)]
    decision = (
        "DESCRIPTIVE_CHECKPOINT_SEPARATOR_PRESENT"
        if len(significant) or model_pvalue <= 0.05
        else "CHECKPOINT_INFORMATION_INSUFFICIENT"
    )
    yearly = (
        rows.groupby("entry_year", sort=True)
        .agg(
            trade_count=("hold_better", "size"),
            hold_better_count=("hold_better", "sum"),
            mean_hold_advantage=("hold_advantage", "mean"),
            median_hold_advantage=("hold_advantage", "median"),
        )
        .reset_index()
    )
    evidence = {
        "schema_version": 1,
        "status": "PASS",
        "experiment_id": EXPERIMENT_ID,
        "credential_id": protocol["credential_id"],
        "strategy_release": "S007-v1",
        "development_cutoff": protocol["development_cutoff"],
        "decision": decision,
        "sample": {
            "trade_count": int(len(rows)),
            "hold_better_count": int(rows["hold_better"].sum()),
            "exit_better_count": int((~rows["hold_better"]).sum()),
            "mean_hold_advantage": float(rows["hold_advantage"].mean()),
            "median_hold_advantage": float(rows["hold_advantage"].median()),
        },
        "univariate": {
            "predictor_count": int(len(contrasts)),
            "fdr_significant_count": int(len(significant)),
            "strongest": contrasts.head(5).replace({np.nan: None}).to_dict(orient="records"),
        },
        "fixed_cross_year_model": {
            "features": model_features,
            "auc": model_auc,
            "discrimination_auc": max(model_auc, 1.0 - model_auc),
            "balanced_accuracy": model_balanced_accuracy,
            "two_sided_permutation_pvalue": model_pvalue,
            "permutation_trials_completed": completed,
            "folds": fold_rows,
        },
        "outcome_uses_post_checkpoint_path": True,
        "predictors_use_post_checkpoint_data": False,
        "forward_data_read": False,
        "new_policy_simulated": False,
        "parameters_searched": False,
        "strategy_parameters_changed": False,
        "candidate_created": False,
        "pte_mutated": False,
        "review_required_before_next_experiment": True,
    }

    artifacts = experiment / "artifacts"
    artifacts.mkdir(exist_ok=True)
    rows.to_csv(
        artifacts / "trigger_mechanism_rows.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    contrasts.to_csv(
        artifacts / "feature_contrasts.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    yearly.to_csv(
        artifacts / "yearly_action_advantage.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    _write_json(artifacts / "mechanism_evidence.json", evidence)

    strongest = contrasts.iloc[0]
    experiment.joinpath("03_execution.md").write_text(
        "# S007 EX43 执行\n\n"
        "状态：`COMPLETE`。43笔EX42触发交易、冻结因果特征和首日分数均通过来源哈希"
        "与分数重建校验。所有解释变量截止买入成交当日收盘，后续路径只用于定义"
        "“持有更好/退出更好”的研究标签。\n\n"
        f"继续持有更好30笔，提前退出更好13笔；平均持有优势为"
        f"{float(rows['hold_advantage'].mean()):.2%}。最强单变量为"
        f"`{strongest['feature']}`，区分AUC {float(strongest['discrimination_auc']):.3f}，"
        f"置换p值{float(strongest['permutation_pvalue']):.4f}，"
        f"BH-FDR q值{float(strongest['bh_fdr_qvalue']):.4f}。\n\n"
        f"固定跨年模型AUC为{model_auc:.3f}，0.5阈值平衡准确率为"
        f"{model_balanced_accuracy:.3f}，双侧标签置换p值为{model_pvalue:.4f}。\n",
        encoding="utf-8",
    )
    if decision == "CHECKPOINT_INFORMATION_INSUFFICIENT":
        interpretation = (
            "首日检查点的预注册变量没有形成经多重校正或跨年验证支持的稳定分层。"
            "继续在同一检查点搜索阈值会有较高过拟合风险。"
        )
    else:
        interpretation = (
            "首日检查点存在描述性分层信号，但它尚未构成动作规则；需要人工评审其"
            "经济含义和跨年稳定性后，才能决定是否预注册新的反事实。"
        )
    experiment.joinpath("04_conclusion.md").write_text(
        "# S007 EX43 结论\n\n"
        f"状态：`COMPLETE`；裁决：`{decision}`，等待人工评审。\n\n"
        f"{interpretation}\n\n"
        f"最强单变量的BH-FDR q值为{float(strongest['bh_fdr_qvalue']):.4f}；"
        f"固定跨年模型AUC为{model_auc:.3f}，双侧置换p值为{model_pvalue:.4f}。"
        "本轮没有搜索阈值、模拟新策略、创建候选、修改冻结策略或改变PTE。"
        "下一实验须经人工评审后再启动。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "strategy_version": protocol["strategy_version"],
            "credential_id": protocol["credential_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": decision,
            "sample_trades": int(len(rows)),
            "strategy_parameters_changed": False,
            "candidate_created": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
