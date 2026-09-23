from __future__ import annotations

from collections import Counter
import hashlib
from importlib.metadata import version
import json
from math import ceil
from pathlib import Path
import time

import numpy as np
import pandas as pd
import statsmodels.api as sm

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX43"


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


def _spearman(left: pd.Series, right: pd.Series) -> float | None:
    frame = pd.concat([left.rename("left"), right.rename("right")], axis=1).dropna()
    if len(frame) < 3 or frame["left"].nunique() < 2 or frame["right"].nunique() < 2:
        return None
    return float(frame["left"].corr(frame["right"], method="spearman"))


def _bh(values: pd.Series) -> pd.Series:
    result = pd.Series(np.nan, index=values.index, dtype=float)
    valid = values.dropna().sort_values()
    if valid.empty:
        return result
    adjusted = valid.to_numpy(dtype=float) * len(valid) / np.arange(1, len(valid) + 1)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    result.loc[valid.index] = np.minimum(adjusted, 1.0)
    return result


def _empirical_score(train: pd.Series, apply: pd.Series, lower: float, upper: float) -> tuple[pd.Series, pd.Series]:
    training = train.dropna().astype(float)
    if len(training) < 3:
        return pd.Series(np.nan, index=train.index), pd.Series(np.nan, index=apply.index)
    low, high = float(training.quantile(lower)), float(training.quantile(upper))
    clipped = training.clip(low, high).sort_values().to_numpy(dtype=float)
    if len(np.unique(clipped)) < 2:
        return pd.Series(np.nan, index=train.index), pd.Series(np.nan, index=apply.index)

    def score(values: pd.Series) -> pd.Series:
        numeric = values.astype(float).clip(low, high)
        ranked = np.searchsorted(clipped, numeric.to_numpy(dtype=float), side="right") / len(clipped)
        output = pd.Series(ranked - 0.5, index=values.index, dtype=float)
        output.loc[values.isna()] = np.nan
        return output

    return score(train), score(apply)


def _residualize(outcome: pd.Series, prices: pd.DataFrame) -> pd.Series:
    close = prices["close"].astype(float)
    past_return = close.pct_change(120, fill_method=None).reindex(outcome.index)
    volatility = close.pct_change(fill_method=None).rolling(60).std(ddof=0).reindex(outcome.index)
    years = pd.get_dummies(outcome.index.year, prefix="year", drop_first=True, dtype=float)
    years.index = outcome.index
    frame = pd.concat(
        [outcome.rename("outcome"), past_return.rename("past_return_120"), volatility.rename("volatility_60"), years],
        axis=1,
    ).dropna()
    fitted = sm.OLS(frame["outcome"].astype(float), sm.add_constant(frame.drop(columns="outcome").astype(float))).fit()
    residual = pd.Series(np.nan, index=outcome.index, dtype=float)
    residual.loc[frame.index] = fitted.resid
    return residual


def _hac(score: pd.Series, outcome: pd.Series, max_lags: int) -> tuple[float | None, float | None]:
    frame = pd.concat([score.rename("score"), outcome.rename("outcome")], axis=1).dropna()
    if len(frame) < 20 or frame["score"].nunique() < 2:
        return None, None
    normalized = (frame["score"] - frame["score"].mean()) / frame["score"].std(ddof=0)
    fitted = sm.OLS(frame["outcome"].to_numpy(dtype=float), sm.add_constant(normalized.to_numpy(dtype=float))).fit(
        cov_type="HAC", cov_kwds={"maxlags": int(max_lags)}
    )
    coefficient, two_sided = float(fitted.params[1]), float(fitted.pvalues[1])
    return coefficient, two_sided / 2 if coefficient >= 0 else 1 - two_sided / 2


def _annual_ics(score: pd.Series, outcome: pd.Series, minimum: int) -> dict[str, float | None]:
    values: dict[str, float | None] = {}
    for year, year_outcome in outcome.groupby(outcome.index.year):
        frame = pd.concat([score.reindex(year_outcome.index), year_outcome], axis=1).dropna()
        values[str(int(year))] = _spearman(frame.iloc[:, 0], frame.iloc[:, 1]) if len(frame) >= minimum else None
    return values


def _bootstrap_probability(score: pd.Series, outcome: pd.Series, block_sessions: int, repetitions: int, seed: int) -> tuple[float, float, float, float]:
    frame = pd.concat([score.rename("score"), outcome.rename("outcome")], axis=1).dropna()
    ranked = frame.rank(method="average", pct=True).to_numpy(dtype=float)
    count, block_count = len(ranked), ceil(len(ranked) / block_sessions)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, count, size=(repetitions, block_count))
    indices = ((starts[:, :, None] + np.arange(block_sessions)[None, None, :]) % count).reshape(repetitions, -1)[:, :count]
    left, right = ranked[indices, 0], ranked[indices, 1]
    left -= left.mean(axis=1, keepdims=True)
    right -= right.mean(axis=1, keepdims=True)
    correlations = (left * right).sum(axis=1) / np.sqrt((left**2).sum(axis=1) * (right**2).sum(axis=1))
    correlations = correlations[np.isfinite(correlations)]
    if len(correlations) < repetitions * 0.95:
        raise ValueError("too many invalid block bootstrap samples")
    return (
        float(np.mean(correlations > 0)),
        float(np.quantile(correlations, 0.05)),
        float(np.quantile(correlations, 0.50)),
        float(np.quantile(correlations, 0.95)),
    )


def _load_prices(repo: Path, expected_manifest_sha256: str) -> pd.DataFrame:
    manifest_path = repo / "data/raw/518880_execution_manifest.json"
    if _sha256(manifest_path) != expected_manifest_sha256:
        raise ValueError("execution manifest differs from frozen identity")
    manifest = _read(manifest_path)
    frames = []
    for year in range(2013, 2025):
        name = f"518880_execution_daily_{year}.csv"
        path = repo / "data/raw" / name
        if _sha256(path) != manifest["files"][name]["sha256"]:
            raise ValueError(f"execution daily file differs from manifest: {name}")
        frames.append(pd.read_csv(path))
    prices = pd.concat(frames, ignore_index=True)
    prices["Date"] = pd.to_datetime(prices.pop("date"), errors="raise")
    prices = prices.sort_values("Date").set_index("Date")
    if prices.index.duplicated().any():
        raise ValueError("execution prices contain duplicate sessions")
    return prices


def main() -> None:
    started = time.perf_counter()
    experiment = Path(__file__).resolve().parent
    repo, artifacts = experiment.parents[2], experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = ("reads_sealed_validation", "input_selection", "template_selection", "starts_search", "candidate_generation", "promotion_allowed", "mutates_catalog", "mutates_platform", "mutates_pte")
    if not protocol.get("reads_development_returns") or any(protocol.get(key) for key in forbidden):
        raise ValueError("long-cycle audit permissions differ from frozen protocol")

    ex16 = repo / "experiments/S008/20260923_S008_EX16"
    ex42 = repo / "experiments/S008/20260923_S008_EX42"
    validate_experiment_archive(ex16)
    validate_experiment_archive(ex42)
    source_paths = {
        "ex16_manifest_sha256": ex16 / "experiment_manifest.json",
        "feature_panel_sha256": ex16 / "artifacts/causal_feature_panel.csv.gz",
        "feature_catalog_sha256": ex16 / "artifacts/feature_catalog.csv",
        "execution_manifest_sha256": repo / "data/raw/518880_execution_manifest.json",
        "ex42_manifest_sha256": ex42 / "experiment_manifest.json",
    }
    for key, path in source_paths.items():
        if _sha256(path) != protocol["sources"][key]:
            raise ValueError(f"frozen source differs: {path}")

    panel = pd.read_csv(source_paths["feature_panel_sha256"])
    panel["Date"] = pd.to_datetime(panel.pop("Date"), errors="raise")
    panel = panel.set_index("Date").sort_index()
    catalog = pd.read_csv(source_paths["feature_catalog_sha256"]).set_index("feature")
    features = list(protocol["eligible_features"])
    if len(features) != int(protocol["feature_count"]) or len(set(features)) != len(features):
        raise ValueError("frozen feature count is invalid")
    missing = sorted(set(features) - set(panel.columns) - set(catalog.index))
    if missing or any(feature not in panel.columns or feature not in catalog.index for feature in features):
        raise ValueError(f"frozen features are unavailable: {missing}")
    panel = panel[features]
    prices = _load_prices(repo, protocol["sources"]["execution_manifest_sha256"])
    if not panel.index.equals(prices.index):
        raise ValueError("feature panel and execution calendar are not identical")

    horizons = [int(value) for value in protocol["horizons_sessions"]]
    primary_horizon = int(protocol["primary_horizon_sessions"])
    segments, statistics = protocol["segments"], protocol["statistics"]
    opens = prices["open"].astype(float)
    session_dates = pd.Series(prices.index, index=prices.index)
    outcomes = {horizon: opens.shift(-(horizon + 1)).div(opens.shift(-1)).sub(1) for horizon in horizons}
    exit_dates = {horizon: session_dates.shift(-(horizon + 1)) for horizon in horizons}
    primary_mask = panel.index.to_series().between(segments["discovery_start"], segments["discovery_end"]) & exit_dates[primary_horizon].le(pd.Timestamp(segments["discovery_end"]))
    primary_outcome = outcomes[primary_horizon].loc[primary_mask].dropna()
    raw_primary_ics = {feature: _spearman(panel.loc[primary_outcome.index, feature], primary_outcome) for feature in features}
    orientations = {feature: -1.0 if raw_primary_ics[feature] is not None and raw_primary_ics[feature] < 0 else 1.0 for feature in features}

    rows: list[dict[str, object]] = []
    for horizon in horizons:
        discovery_mask = panel.index.to_series().between(segments["discovery_start"], segments["discovery_end"]) & exit_dates[horizon].le(pd.Timestamp(segments["discovery_end"]))
        confirmation_mask = panel.index.to_series().between(segments["confirmation_start"], segments["confirmation_end"]) & exit_dates[horizon].le(pd.Timestamp(segments["confirmation_end"]))
        discovery_outcome = outcomes[horizon].loc[discovery_mask].dropna()
        confirmation_outcome = outcomes[horizon].loc[confirmation_mask].dropna()
        residual_outcome = _residualize(confirmation_outcome, prices)
        for ordinal, feature in enumerate(features):
            discovery_score, confirmation_score = _empirical_score(panel.loc[discovery_outcome.index, feature], panel.loc[confirmation_outcome.index, feature], float(statistics["winsor_lower"]), float(statistics["winsor_upper"]))
            discovery_score *= orientations[feature]
            confirmation_score *= orientations[feature]
            discovery_frame = pd.concat([discovery_score, discovery_outcome], axis=1).dropna()
            confirmation_frame = pd.concat([confirmation_score.rename("score"), confirmation_outcome.rename("outcome")], axis=1).dropna()
            identifiable = bool(
                len(discovery_frame) >= int(statistics["minimum_discovery_observations"])
                and len(confirmation_frame) >= int(statistics["minimum_confirmation_observations"])
                and confirmation_frame["score"].nunique() >= int(statistics["minimum_distinct_confirmation_values"])
            )
            coefficient, pvalue = _hac(confirmation_score, confirmation_outcome, horizon) if identifiable else (None, None)
            annual = _annual_ics(confirmation_score, confirmation_outcome, int(statistics["minimum_annual_observations"]))
            positive_years = sum(value is not None and value > 0 for value in annual.values())
            bootstrap = _bootstrap_probability(
                confirmation_score,
                confirmation_outcome,
                int(statistics["bootstrap_block_sessions"]),
                int(statistics["bootstrap_repetitions"]),
                int(statistics["bootstrap_seed"]) + horizon * 1000 + ordinal,
            ) if identifiable else (None, None, None, None)
            if confirmation_frame["score"].nunique() >= 5:
                low, high = confirmation_frame["score"].quantile(0.2), confirmation_frame["score"].quantile(0.8)
                spread = float(confirmation_frame.loc[confirmation_frame["score"] >= high, "outcome"].mean() - confirmation_frame.loc[confirmation_frame["score"] <= low, "outcome"].mean())
            else:
                spread = None
            rows.append({
                "path_id": f"{feature}:H{horizon}",
                "feature": feature,
                "hypothesis_id": catalog.loc[feature, "hypothesis_id"],
                "information_family": catalog.loc[feature, "information_family"],
                "financial_role": catalog.loc[feature, "financial_role"],
                "horizon_sessions": horizon,
                "orientation": "POSITIVE" if orientations[feature] > 0 else "NEGATIVE",
                "raw_primary_discovery_ic": raw_primary_ics[feature],
                "discovery_observations": len(discovery_frame),
                "confirmation_observations": len(confirmation_frame),
                "discovery_ic": _spearman(discovery_score, discovery_outcome),
                "confirmation_ic": _spearman(confirmation_score, confirmation_outcome),
                "confirmation_residual_ic": _spearman(confirmation_score, residual_outcome),
                "hac_coefficient": coefficient,
                "hac_one_sided_pvalue": pvalue,
                "positive_confirmation_years": positive_years,
                "annual_ics": json.dumps(annual, ensure_ascii=False, allow_nan=False),
                "top_minus_bottom_return": spread,
                "bootstrap_positive_probability": bootstrap[0],
                "bootstrap_ic_lower_90": bootstrap[1],
                "bootstrap_ic_median": bootstrap[2],
                "bootstrap_ic_upper_90": bootstrap[3],
                "identifiable": identifiable,
            })

    results = pd.DataFrame(rows)
    expected = int(protocol["preregistered_path_count"])
    if len(results) != expected or results["path_id"].nunique() != expected:
        raise ValueError(f"information path count differs: {len(results)} != {expected}")
    results["global_bh_qvalue"] = _bh(results["hac_one_sided_pvalue"])
    results["positive_horizon_count"] = results.groupby("feature")["confirmation_ic"].transform(lambda values: int(values.gt(0).sum()))
    stable = (
        results["identifiable"]
        & results["raw_primary_discovery_ic"].abs().ge(float(statistics["discovery_absolute_ic_minimum"]))
        & results["confirmation_ic"].gt(0)
        & results["confirmation_residual_ic"].gt(0)
        & results["positive_confirmation_years"].ge(int(statistics["minimum_positive_confirmation_years"]))
        & results["positive_horizon_count"].ge(int(statistics["minimum_positive_horizons"]))
        & results["top_minus_bottom_return"].gt(0)
        & results["bootstrap_positive_probability"].ge(float(statistics["directional_bootstrap_probability"]))
    )
    results["evidence_label"] = "NO_STABLE_EVIDENCE"
    results.loc[~results["identifiable"], "evidence_label"] = "UNIDENTIFIABLE"
    results.loc[stable, "evidence_label"] = "DIRECTIONALLY_STABLE"
    results.loc[stable & results["hac_one_sided_pvalue"].le(float(statistics["nominal_one_sided_alpha"])) & results["bootstrap_positive_probability"].ge(float(statistics["nominal_bootstrap_probability"])), "evidence_label"] = "NOMINAL_SUPPORT"
    results.loc[stable & results["global_bh_qvalue"].le(float(statistics["benjamini_hochberg_fdr"])) & results["bootstrap_positive_probability"].ge(float(statistics["fdr_bootstrap_probability"])), "evidence_label"] = "FDR_SUPPORTED"
    results = results.sort_values(["evidence_label", "global_bh_qvalue", "confirmation_residual_ic"], ascending=[True, True, False]).reset_index(drop=True)

    supported_labels = {"FDR_SUPPORTED", "NOMINAL_SUPPORT", "DIRECTIONALLY_STABLE"}
    primary = results.loc[results["horizon_sessions"] == primary_horizon]
    primary_supported = primary.loc[primary["evidence_label"].isin(supported_labels)]
    decision = "PROCEED_TO_LONG_CYCLE_COMPONENT_REVIEW" if len(primary_supported) else "STOP_LONG_CYCLE_NO_STABLE_INFORMATION"
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    results.to_csv(artifacts / "long_cycle_path_ledger.csv.gz", index=False, compression=compression, lineterminator="\n")
    primary_supported.to_csv(artifacts / "primary_supported_paths.csv", index=False, encoding="utf-8", lineterminator="\n")
    family_summary = results.groupby(["hypothesis_id", "information_family", "horizon_sessions"], as_index=False).agg(
        paths=("path_id", "size"),
        identifiable=("identifiable", "sum"),
        median_confirmation_ic=("confirmation_ic", "median"),
        median_residual_ic=("confirmation_residual_ic", "median"),
        stable_paths=("evidence_label", lambda values: int(values.isin(supported_labels).sum())),
        best_global_qvalue=("global_bh_qvalue", "min"),
    ).sort_values(["hypothesis_id", "information_family", "horizon_sessions"])
    family_summary.to_csv(artifacts / "family_horizon_summary.csv", index=False, encoding="utf-8", lineterminator="\n")
    label_counts = Counter(results["evidence_label"])
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "decision": decision,
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "feature_count": results["feature"].nunique(),
        "path_count": len(results),
        "primary_horizon_sessions": primary_horizon,
        "evidence_label_counts": dict(sorted(label_counts.items())),
        "primary_supported_count": len(primary_supported),
        "primary_supported_by_hypothesis": {str(key): int(value) for key, value in primary_supported.groupby("hypothesis_id").size().sort_index().items()},
        "statsmodels_version": version("statsmodels"),
        "reads_sealed_validation": False,
        "input_selected": False,
        "template_selected": False,
        "search_started": False,
        "candidate_created": False,
        "catalog_mutated": False,
        "platform_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "long_cycle_information_audit.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S008 EX43 执行记录\n\n"
        f"48项预注册慢变量在120/252日两个周期形成{len(results)}条信息路径，全部进入统计总账。"
        "方向仅由发现期252日主标签确定；未读取封存池、选择策略输入、实例化原型或启动搜索。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX43 结论\n\n"
        f"裁决：`{decision}`。96条路径中，全局FDR支持{label_counts.get('FDR_SUPPORTED', 0)}条、"
        f"名义支持{label_counts.get('NOMINAL_SUPPORT', 0)}条、方向稳定{label_counts.get('DIRECTIONALLY_STABLE', 0)}条；"
        f"252日主周期可进入冗余与职责审查的路径共{len(primary_supported)}条。\n\n"
        "这些结果只说明开发池内存在或不存在可重复的长期方向信息，不构成可交易机制、策略参数或候选证据。\n",
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
