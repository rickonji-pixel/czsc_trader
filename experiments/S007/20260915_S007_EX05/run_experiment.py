from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd
import statsmodels.api as sm

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX05"


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
    count = len(valid)
    adjusted = valid.to_numpy(dtype=float) * count / np.arange(1, count + 1, dtype=float)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    result.loc[valid.index] = np.minimum(adjusted, 1.0)
    return result


def _empirical_score(train: pd.Series, apply: pd.Series, lower: float, upper: float) -> tuple[pd.Series, pd.Series]:
    training = train.dropna().astype(float)
    if len(training) < 3:
        return pd.Series(np.nan, index=train.index), pd.Series(np.nan, index=apply.index)
    low = float(training.quantile(lower))
    high = float(training.quantile(upper))
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


def _hac(score: pd.Series, outcome: pd.Series, max_lags: int) -> tuple[float | None, float | None]:
    frame = pd.concat([score.rename("score"), outcome.rename("outcome")], axis=1).dropna()
    if len(frame) < 20 or frame["score"].nunique() < 2:
        return None, None
    standard_deviation = float(frame["score"].std(ddof=0))
    if standard_deviation <= 0.0:
        return None, None
    normalized = (frame["score"] - frame["score"].mean()) / standard_deviation
    fitted = sm.OLS(
        frame["outcome"].to_numpy(dtype=float),
        sm.add_constant(normalized.to_numpy(dtype=float)),
    ).fit(cov_type="HAC", cov_kwds={"maxlags": int(max_lags)})
    coefficient = float(fitted.params[1])
    two_sided = float(fitted.pvalues[1])
    one_sided = two_sided / 2.0 if coefficient >= 0 else 1.0 - two_sided / 2.0
    return coefficient, one_sided


def _residualize(outcome: pd.Series, prices: pd.DataFrame) -> pd.Series:
    close = prices["close"].astype(float)
    past_return = close.pct_change(5, fill_method=None).reindex(outcome.index)
    volatility = close.pct_change(fill_method=None).rolling(20).std().reindex(outcome.index)
    years = pd.get_dummies(outcome.index.year, prefix="year", drop_first=True, dtype=float)
    years.index = outcome.index
    frame = pd.concat(
        [outcome.rename("outcome"), past_return.rename("past_return"), volatility.rename("volatility"), years],
        axis=1,
    ).dropna()
    fitted = sm.OLS(
        frame["outcome"].astype(float),
        sm.add_constant(frame.drop(columns="outcome").astype(float)),
    ).fit()
    residual = pd.Series(np.nan, index=outcome.index, dtype=float)
    residual.loc[frame.index] = fitted.resid
    return residual


def _annual_ics(score: pd.Series, outcome: pd.Series, minimum: int) -> dict[str, float | None]:
    values: dict[str, float | None] = {}
    for year, year_outcome in outcome.groupby(outcome.index.year):
        frame = pd.concat([score.reindex(year_outcome.index), year_outcome], axis=1).dropna()
        values[str(int(year))] = _spearman(frame.iloc[:, 0], frame.iloc[:, 1]) if len(frame) >= minimum else None
    return values


def _block_bootstrap(
    score: pd.Series,
    outcome: pd.Series,
    *,
    block_sessions: int,
    repetitions: int,
    seed: int,
) -> tuple[float | None, float | None, float | None, float | None]:
    frame = pd.concat([score.rename("score"), outcome.rename("outcome")], axis=1)
    if frame.dropna().shape[0] < 20:
        return None, None, None, None
    ranked = frame.rank(method="average", pct=True).to_numpy(dtype=float)
    count = len(ranked)
    block_count = int(np.ceil(count / block_sessions))
    rng = np.random.default_rng(seed)
    draws: list[float] = []
    offsets = np.arange(block_sessions)
    for _ in range(repetitions):
        starts = rng.integers(0, count, size=block_count)
        indices = ((starts[:, None] + offsets[None, :]) % count).ravel()[:count]
        sample = ranked[indices]
        valid = np.isfinite(sample).all(axis=1)
        if valid.sum() < 20:
            continue
        correlation = np.corrcoef(sample[valid, 0], sample[valid, 1])[0, 1]
        if np.isfinite(correlation):
            draws.append(float(correlation))
    if len(draws) < repetitions * 0.95:
        raise ValueError("too many invalid block bootstrap samples")
    array = np.asarray(draws, dtype=float)
    return (
        float(np.mean(array > 0.0)),
        float(np.quantile(array, 0.05)),
        float(np.quantile(array, 0.50)),
        float(np.quantile(array, 0.95)),
    )


def _raw_daily(repo: Path, hashes: dict[str, str]) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for relative, expected in hashes.items():
        path = repo / relative
        if _sha256(path) != expected:
            raise ValueError(f"daily source differs from frozen protocol: {relative}")
        rows.append(pd.read_csv(path))
    frame = pd.concat(rows, ignore_index=True)
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    frame = frame.sort_values("date").drop_duplicates("date", keep="last")
    return frame.set_index("date")


def main() -> None:
    started = time.perf_counter()
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if not protocol.get("reads_new_returns"):
        raise ValueError("information value audit must explicitly declare return access")
    forbidden = ("input_selection", "template_selection", "candidate_generation", "promotion_allowed", "mutates_catalog", "mutates_strategy_manager", "mutates_pte")
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX05 cannot select, instantiate, promote, or deploy a strategy")

    sources = protocol["sources"]
    ex04 = repo / str(sources["ex04_archive"])
    validate_experiment_archive(ex04)
    frozen_files = {
        ex04 / "experiment_manifest.json": sources["ex04_manifest_sha256"],
        ex04 / "artifacts/causal_feature_panel.csv.gz": sources["feature_panel_sha256"],
        ex04 / "artifacts/feature_catalog.csv": sources["feature_catalog_sha256"],
    }
    for path, expected in frozen_files.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen EX04 source differs: {path}")

    panel = pd.read_csv(ex04 / "artifacts/causal_feature_panel.csv.gz")
    panel["date"] = pd.to_datetime(panel["date"], errors="raise")
    panel = panel.set_index("date").sort_index()
    catalog = pd.read_csv(ex04 / "artifacts/feature_catalog.csv").set_index("feature")
    if set(panel.columns) != set(catalog.index) or len(panel.columns) != int(protocol["feature_count"]):
        raise ValueError("feature panel and catalog identities differ from protocol")

    prices = _raw_daily(repo, sources["daily_sha256"])
    cutoff = pd.Timestamp(protocol["development_cutoff"])
    prices = prices.loc[prices.index <= cutoff]
    if not panel.index.equals(prices.index):
        raise ValueError("feature panel and daily calendar are not identical")

    segments = protocol["segments"]
    labels = protocol["labels"]
    horizons = [int(labels["primary_horizon_sessions"]), *map(int, labels["diagnostic_horizons_sessions"])]
    horizons = list(dict.fromkeys(horizons))
    opens = prices["open"].astype(float)
    session_dates = pd.Series(prices.index, index=prices.index)
    outcomes: dict[int, pd.Series] = {}
    exit_dates: dict[int, pd.Series] = {}
    for horizon in horizons:
        outcomes[horizon] = opens.shift(-(horizon + 1)).div(opens.shift(-1)).sub(1.0)
        exit_dates[horizon] = session_dates.shift(-(horizon + 1))

    model = protocol["model"]
    statistics = protocol["statistics"]
    result_rows: list[dict[str, object]] = []
    for horizon in horizons:
        outcome = outcomes[horizon]
        exit_date = exit_dates[horizon]
        discovery_mask = (
            panel.index.to_series().between(segments["discovery_start"], segments["discovery_end"])
            & exit_date.le(pd.Timestamp(segments["discovery_end"]))
        )
        confirmation_mask = (
            panel.index.to_series().between(segments["confirmation_start"], segments["confirmation_end"])
            & exit_date.le(pd.Timestamp(segments["confirmation_end"]))
        )
        discovery_outcome = outcome.loc[discovery_mask].dropna()
        confirmation_outcome = outcome.loc[confirmation_mask].dropna()
        confirmation_residual = _residualize(confirmation_outcome, prices)

        for ordinal, feature in enumerate(panel.columns):
            discovery_raw = panel.loc[discovery_outcome.index, feature]
            confirmation_raw = panel.loc[confirmation_outcome.index, feature]
            discovery_score, confirmation_score = _empirical_score(
                discovery_raw,
                confirmation_raw,
                float(model["winsor_lower"]),
                float(model["winsor_upper"]),
            )
            raw_discovery_ic = _spearman(discovery_score, discovery_outcome)
            orientation = -1.0 if raw_discovery_ic is not None and raw_discovery_ic < 0 else 1.0
            discovery_score *= orientation
            confirmation_score *= orientation
            discovery_ic = _spearman(discovery_score, discovery_outcome)
            confirmation_ic = _spearman(confirmation_score, confirmation_outcome)
            residual_ic = _spearman(confirmation_score, confirmation_residual)
            discovery_observations = int(pd.concat([discovery_score, discovery_outcome], axis=1).dropna().shape[0])
            confirmation_frame = pd.concat(
                [confirmation_score.rename("score"), confirmation_outcome.rename("outcome")], axis=1
            ).dropna()
            confirmation_observations = int(len(confirmation_frame))
            identifiable = (
                discovery_observations >= int(model["minimum_discovery_observations"])
                and confirmation_observations >= int(model["minimum_confirmation_observations"])
                and confirmation_frame["score"].nunique() >= int(model["minimum_distinct_confirmation_values"])
            )
            coefficient, pvalue = _hac(
                confirmation_score, confirmation_outcome, int(statistics["hac_max_lags"])
            ) if identifiable else (None, None)
            annual = _annual_ics(
                confirmation_score,
                confirmation_outcome,
                int(model["minimum_annual_observations"]),
            )
            positive_years = sum(value is not None and value > 0 for value in annual.values())
            bootstrap = _block_bootstrap(
                confirmation_score,
                confirmation_outcome,
                block_sessions=int(statistics["bootstrap_block_sessions"]),
                repetitions=int(statistics["bootstrap_repetitions"]),
                seed=int(statistics["bootstrap_seed"]) + horizon * 1000 + ordinal,
            ) if identifiable else (None, None, None, None)
            if confirmation_frame["score"].nunique() >= 5:
                low = float(confirmation_frame["score"].quantile(0.2))
                high = float(confirmation_frame["score"].quantile(0.8))
                top = confirmation_frame.loc[confirmation_frame["score"].ge(high), "outcome"]
                bottom = confirmation_frame.loc[confirmation_frame["score"].le(low), "outcome"]
                spread = float(top.mean() - bottom.mean()) if len(top) and len(bottom) else None
            else:
                spread = None
            result_rows.append(
                {
                    "path_id": f"{feature}:H{horizon}",
                    "feature": feature,
                    "hypothesis_id": catalog.loc[feature, "hypothesis_id"],
                    "information_family": catalog.loc[feature, "information_family"],
                    "provider": catalog.loc[feature, "provider"],
                    "horizon_sessions": horizon,
                    "orientation": "POSITIVE" if orientation > 0 else "NEGATIVE",
                    "discovery_observations": discovery_observations,
                    "confirmation_observations": confirmation_observations,
                    "discovery_ic": discovery_ic,
                    "confirmation_ic": confirmation_ic,
                    "confirmation_residual_ic": residual_ic,
                    "hac_coefficient": coefficient,
                    "hac_one_sided_pvalue": pvalue,
                    "positive_confirmation_years": int(positive_years),
                    "annual_ics": json.dumps(annual, ensure_ascii=False, allow_nan=False),
                    "top_minus_bottom_return": spread,
                    "bootstrap_positive_probability": bootstrap[0],
                    "bootstrap_ic_lower_90": bootstrap[1],
                    "bootstrap_ic_median": bootstrap[2],
                    "bootstrap_ic_upper_90": bootstrap[3],
                    "identifiable": bool(identifiable),
                }
            )

    results = pd.DataFrame(result_rows)
    expected = int(protocol["preregistered_path_count"])
    if len(results) != expected or results["path_id"].nunique() != expected:
        raise ValueError(f"full information path count differs: {len(results)} != {expected}")
    results["horizon_bh_qvalue"] = np.nan
    for _, indices in results.groupby("horizon_sessions").groups.items():
        results.loc[indices, "horizon_bh_qvalue"] = _bh(results.loc[indices, "hac_one_sided_pvalue"])
    results["global_bh_qvalue"] = _bh(results["hac_one_sided_pvalue"])

    stable = (
        results["identifiable"]
        & results["discovery_ic"].ge(float(statistics["discovery_absolute_ic_minimum"]))
        & results["confirmation_ic"].gt(0)
        & results["confirmation_residual_ic"].gt(0)
        & results["positive_confirmation_years"].ge(int(statistics["minimum_positive_confirmation_years"]))
        & results["bootstrap_positive_probability"].ge(float(statistics["directional_bootstrap_probability"]))
    )
    results["evidence_label"] = "NO_STABLE_EVIDENCE"
    results.loc[~results["identifiable"], "evidence_label"] = "UNIDENTIFIABLE"
    results.loc[stable, "evidence_label"] = "DIRECTIONALLY_STABLE"
    results.loc[
        stable
        & results["hac_one_sided_pvalue"].le(float(statistics["nominal_one_sided_alpha"]))
        & results["bootstrap_positive_probability"].ge(float(statistics["nominal_bootstrap_probability"])),
        "evidence_label",
    ] = "NOMINAL_SUPPORT"
    results.loc[
        stable
        & results["global_bh_qvalue"].le(float(statistics["benjamini_hochberg_fdr"]))
        & results["bootstrap_positive_probability"].ge(float(statistics["fdr_bootstrap_probability"])),
        "evidence_label",
    ] = "FDR_SUPPORTED"
    results = results.sort_values(
        ["evidence_label", "global_bh_qvalue", "confirmation_residual_ic"],
        ascending=[True, True, False],
    ).reset_index(drop=True)

    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    results.to_csv(
        artifacts / "information_path_ledger.csv.gz",
        index=False,
        compression=compression,
        lineterminator="\n",
    )
    summary = (
        results.groupby(["hypothesis_id", "information_family", "horizon_sessions"], as_index=False)
        .agg(
            paths=("path_id", "size"),
            identifiable=("identifiable", "sum"),
            median_confirmation_ic=("confirmation_ic", "median"),
            median_residual_ic=("confirmation_residual_ic", "median"),
            stable_paths=("evidence_label", lambda values: int(values.isin({"FDR_SUPPORTED", "NOMINAL_SUPPORT", "DIRECTIONALLY_STABLE"}).sum())),
            best_global_qvalue=("global_bh_qvalue", "min"),
        )
        .sort_values(["hypothesis_id", "information_family", "horizon_sessions"])
    )
    summary.to_csv(artifacts / "family_horizon_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    supported = results.loc[
        results["evidence_label"].isin({"FDR_SUPPORTED", "NOMINAL_SUPPORT", "DIRECTIONALLY_STABLE"})
    ]
    supported.to_csv(artifacts / "supported_information_paths.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    counts = results["evidence_label"].value_counts().sort_index()
    primary = results.loc[results["horizon_sessions"].eq(int(labels["primary_horizon_sessions"]))]
    primary_supported = primary.loc[
        primary["evidence_label"].isin({"FDR_SUPPORTED", "NOMINAL_SUPPORT", "DIRECTIONALLY_STABLE"})
    ]
    decision = "PROCEED_TO_REDUNDANCY_AND_ROLE_REVIEW" if len(primary_supported) else "STOP_PRIMARY_HORIZON_NO_STABLE_INFORMATION"
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": decision,
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "feature_count": int(results["feature"].nunique()),
        "path_count": int(len(results)),
        "primary_horizon_sessions": int(labels["primary_horizon_sessions"]),
        "evidence_label_counts": {str(key): int(value) for key, value in counts.items()},
        "primary_supported_count": int(len(primary_supported)),
        "primary_supported_by_hypothesis": {
            str(key): int(value)
            for key, value in primary_supported.groupby("hypothesis_id").size().sort_index().items()
        },
        "frequency_policy": protocol["frequency_policy"],
        "input_selected": False,
        "template_selected": False,
        "search_started": False,
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
        "protocol_sha256": _sha256(protocol_path),
    }
    _write(artifacts / "information_audit.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S007 EX05 执行\n\n"
        f"状态：`COMPLETE`。101项特征在1/3/5日三个周期形成{len(results)}条预注册路径，"
        "全部进入统计总账。没有选择输入、实例化模板、启动搜索或生成候选。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX05 结论\n\n"
        f"裁决：`{decision}`。303条路径中，全局FDR支持"
        f"{int(results['evidence_label'].eq('FDR_SUPPORTED').sum())}条、名义支持"
        f"{int(results['evidence_label'].eq('NOMINAL_SUPPORT').sum())}条、方向稳定"
        f"{int(results['evidence_label'].eq('DIRECTIONALLY_STABLE').sum())}条；主周期可进入下一步的路径"
        f"{len(primary_supported)}条。\n\n"
        "这些结果只回答单项信息是否具有可重复方向。下一步先去冗余并评审金融职责，仍不得把单项"
        "信息直接当作策略候选。\n",
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
            "decision": decision,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
