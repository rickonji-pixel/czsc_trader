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

from dataflows import DataRequest, DataStatus, Dataflows
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX50"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


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


def _bootstrap_probability(score: pd.Series, outcome: pd.Series, block: int, repetitions: int, seed: int) -> float:
    frame = pd.concat([score.rename("score"), outcome.rename("outcome")], axis=1).dropna().rank(method="average", pct=True)
    values = frame.to_numpy(dtype=float)
    count, block_count = len(values), ceil(len(values) / block)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, count, size=(repetitions, block_count))
    indices = ((starts[:, :, None] + np.arange(block)[None, None, :]) % count).reshape(repetitions, -1)[:, :count]
    left, right = values[indices, 0], values[indices, 1]
    left -= left.mean(axis=1, keepdims=True)
    right -= right.mean(axis=1, keepdims=True)
    correlations = (left * right).sum(axis=1) / np.sqrt((left**2).sum(axis=1) * (right**2).sum(axis=1))
    correlations = correlations[np.isfinite(correlations)]
    if len(correlations) < repetitions * 0.95:
        raise ValueError("too many invalid bootstrap samples")
    return float(np.mean(correlations > 0))


def _fetch(dataflows: Dataflows, specification: dict[str, object], repo: Path):
    result = dataflows.fetch(DataRequest(
        dataset=str(specification["dataset"]),
        symbol=None if specification["symbol"] is None else str(specification["symbol"]),
        start=str(specification["start"]),
        end=str(specification["cutoff"]),
        required_cutoff=str(specification["cutoff"]),
        frequency="daily",
        options={"env_file": repo / ".env"},
    ))
    if result.status is not DataStatus.READY:
        code = result.error.code if result.error else "UNKNOWN"
        raise RuntimeError(f"DFLS {specification['dataset']} is {result.status.value}: {code}")
    return result


def _frame(result) -> pd.DataFrame:
    frame = result.dataframe.copy()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="raise")
    frame = frame.sort_values("Date").set_index("Date")
    if frame.index.duplicated().any():
        raise ValueError(f"duplicate dates in {result.identity.dataset}")
    return frame


def main() -> None:
    started = time.perf_counter()
    experiment = Path(__file__).resolve().parent
    repo, artifacts = experiment.parents[2], experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = ("reads_sealed_validation", "input_selection", "template_selection", "starts_search", "candidate_generation", "promotion_allowed", "mutates_catalog", "mutates_platform", "mutates_pte")
    if not protocol.get("reads_development_returns") or any(protocol.get(key) for key in forbidden):
        raise ValueError("cross-era audit permissions differ from frozen protocol")

    source_paths = {
        "family_sha256": repo / "research/registrations/S008/family.json",
        "materials_sha256": repo / "research/S008/materials.json",
        "ex49_manifest_sha256": repo / "experiments/S008/20260923_S008_EX49/experiment_manifest.json",
        "dfls_contract_sha256": repo / "packages/dataflows/src/dataflows/contract.py",
        "dfls_facade_sha256": repo / "packages/dataflows/src/dataflows/facade.py",
        "dfls_adapter_sha256": repo / "packages/dataflows/src/dataflows/tushare_strategy_data.py",
    }
    for key, path in source_paths.items():
        if _sha256(path) != str(protocol["sources"][key]):
            raise ValueError(f"frozen source differs: {path}")

    dataflows = Dataflows()
    results = {key: _fetch(dataflows, dict(value), repo) for key, value in protocol["datasets"].items()}
    gold, equity, real_yield, etf = (_frame(results[key]) for key in ("gold", "equity", "real_yield", "etf"))
    data_gate_pass = bool(
        len(gold) >= 5390 and len(equity) >= 5380 and len(real_yield) >= 5490 and len(etf) >= 2780
        and gold.index.min() == pd.Timestamp("2002-10-31") and gold.index.max() == pd.Timestamp("2024-12-31")
        and equity.index.min() == pd.Timestamp("2002-10-31") and equity.index.max() == pd.Timestamp("2024-12-31")
        and real_yield.index.min() == pd.Timestamp("2003-01-02") and real_yield.index.max() == pd.Timestamp("2024-12-31")
        and etf.index.min() == pd.Timestamp("2013-07-29") and etf.index.max() == pd.Timestamp("2024-12-31")
    )
    if not data_gate_pass:
        raise ValueError(protocol["adjudication"]["data_failure"])

    gold_close = pd.to_numeric(gold["Close"], errors="raise")
    gold_return = gold_close.pct_change(fill_method=None)
    equity_close = pd.to_numeric(equity["Close"], errors="raise").reindex(gold.index, method="ffill")
    equity_return = equity_close.pct_change(fill_method=None)
    real_yield_10y = pd.to_numeric(real_yield["RealYield10YPercent"], errors="raise").reindex(gold.index, method="ffill").shift(1)
    gold_return_20 = gold_close.pct_change(20, fill_method=None)
    gold_drawdown_120 = gold_close.div(gold_close.rolling(120).max()).sub(1)
    panel = pd.DataFrame(index=gold.index)
    panel["gold_return_120"] = gold_close.pct_change(120, fill_method=None)
    panel["gold_return_252"] = gold_close.pct_change(252, fill_method=None)
    panel["gold_trend_distance_120"] = gold_close.div(gold_close.rolling(120).mean()).sub(1)
    panel["gold_signed_efficiency_120"] = panel["gold_return_120"].div(gold_return.abs().rolling(120).sum())
    panel["real_yield_10y_level"] = real_yield_10y
    panel["real_yield_10y_change_20"] = real_yield_10y.diff(20)
    panel["gold_drawdown_120"] = gold_drawdown_120
    panel["gold_recovery_20_from_drawdown"] = gold_return_20.clip(lower=0).mul((-gold_drawdown_120.shift(20)).clip(lower=0))
    panel["gold_volatility_ratio_20_60"] = gold_return.rolling(20).std(ddof=0).div(gold_return.rolling(60).std(ddof=0))
    panel["sse_return_20"] = equity_close.pct_change(20, fill_method=None)
    panel["sse_drawdown_60"] = equity_close.div(equity_close.rolling(60).max()).sub(1)
    panel["sse_volatility_20"] = equity_return.rolling(20).std(ddof=0)
    panel["gold_minus_sse_return_60"] = gold_close.pct_change(60, fill_method=None).sub(equity_close.pct_change(60, fill_method=None))

    feature_specs = {str(item["name"]): dict(item) for item in protocol["features"]}
    if set(panel.columns) != set(feature_specs) or len(feature_specs) != 13:
        raise ValueError("feature implementation differs from frozen protocol")
    horizons = [int(value) for value in protocol["horizons_sessions"]]
    primary_horizon = int(protocol["primary_horizon_sessions"])
    segments, statistics = protocol["segments"], protocol["statistics"]
    sge_outcomes = {h: gold_close.shift(-h).div(gold_close).sub(1) for h in horizons}
    sge_exit_dates = {h: pd.Series(gold.index, index=gold.index).shift(-h) for h in horizons}
    etf_close = pd.to_numeric(etf["Close"], errors="raise")
    etf_outcomes = {h: etf_close.shift(-h).div(etf_close).sub(1).reindex(gold.index) for h in horizons}
    etf_exit_dates = {h: pd.Series(etf.index, index=etf.index).shift(-h).reindex(gold.index) for h in horizons}

    rows: list[dict[str, object]] = []
    for horizon in horizons:
        discovery_mask = panel.index.to_series().between(segments["discovery_start"], segments["discovery_end"]) & sge_exit_dates[horizon].le(pd.Timestamp(segments["discovery_end"]))
        confirmation_mask = panel.index.to_series().between(segments["confirmation_start"], segments["confirmation_end"]) & sge_exit_dates[horizon].le(pd.Timestamp(segments["confirmation_end"]))
        etf_mask = confirmation_mask & etf_exit_dates[horizon].le(pd.Timestamp(segments["confirmation_end"]))
        discovery_outcome = sge_outcomes[horizon].loc[discovery_mask]
        confirmation_outcome = sge_outcomes[horizon].loc[confirmation_mask]
        etf_outcome = etf_outcomes[horizon].loc[etf_mask]
        for ordinal, (feature, specification) in enumerate(feature_specs.items()):
            score = panel[feature] * float(specification["orientation"])
            discovery_frame = pd.concat([score.loc[discovery_mask], discovery_outcome], axis=1).dropna()
            confirmation_frame = pd.concat([score.loc[confirmation_mask], confirmation_outcome], axis=1).dropna()
            etf_frame = pd.concat([score.loc[etf_mask], etf_outcome], axis=1).dropna()
            identifiable = bool(
                len(discovery_frame) >= int(statistics["minimum_discovery_observations"])
                and len(confirmation_frame) >= int(statistics["minimum_confirmation_observations"])
                and len(etf_frame) >= int(statistics["minimum_etf_bridge_observations"])
                and score.nunique(dropna=True) >= 20
            )
            annual = _annual_ics(score.loc[etf_mask], etf_outcome, int(statistics["minimum_annual_observations"]))
            positive_years = sum(value is not None and value > 0 for value in annual.values())
            coefficient, pvalue = _hac(score.loc[etf_mask], etf_outcome, horizon) if identifiable else (None, None)
            bootstrap = _bootstrap_probability(score.loc[etf_mask], etf_outcome, int(statistics["bootstrap_block_sessions"]), int(statistics["bootstrap_repetitions"]), int(statistics["bootstrap_seed"]) + horizon * 1000 + ordinal) if identifiable else None
            spread = None
            if identifiable:
                low, high = etf_frame.iloc[:, 0].quantile(0.2), etf_frame.iloc[:, 0].quantile(0.8)
                spread = float(etf_frame.loc[etf_frame.iloc[:, 0] >= high].iloc[:, 1].mean() - etf_frame.loc[etf_frame.iloc[:, 0] <= low].iloc[:, 1].mean())
            rows.append({
                "path_id": f"{feature}:H{horizon}",
                "feature": feature,
                "group": specification["group"],
                "family": specification["family"],
                "horizon_sessions": horizon,
                "orientation": int(specification["orientation"]),
                "discovery_observations": len(discovery_frame),
                "confirmation_observations": len(confirmation_frame),
                "etf_bridge_observations": len(etf_frame),
                "discovery_sge_ic": _spearman(discovery_frame.iloc[:, 0], discovery_frame.iloc[:, 1]),
                "confirmation_sge_ic": _spearman(confirmation_frame.iloc[:, 0], confirmation_frame.iloc[:, 1]),
                "confirmation_etf_ic": _spearman(etf_frame.iloc[:, 0], etf_frame.iloc[:, 1]),
                "positive_confirmation_years": positive_years,
                "annual_etf_ics": json.dumps(annual, ensure_ascii=False, allow_nan=False),
                "etf_top_minus_bottom_return": spread,
                "hac_coefficient": coefficient,
                "hac_one_sided_pvalue": pvalue,
                "bootstrap_positive_probability": bootstrap,
                "identifiable": identifiable,
            })

    ledger = pd.DataFrame(rows)
    if len(ledger) != len(feature_specs) * len(horizons) or ledger["path_id"].nunique() != len(ledger):
        raise ValueError("information path count differs from frozen protocol")
    ledger["global_bh_qvalue"] = _bh(ledger["hac_one_sided_pvalue"])
    ledger["positive_horizon_count"] = ledger.groupby("feature")["confirmation_etf_ic"].transform(lambda values: int(values.gt(0).sum()))
    stable = (
        ledger["identifiable"]
        & ledger["discovery_sge_ic"].ge(float(statistics["minimum_oriented_ic"]))
        & ledger["confirmation_sge_ic"].ge(float(statistics["minimum_oriented_ic"]))
        & ledger["confirmation_etf_ic"].ge(float(statistics["minimum_oriented_ic"]))
        & ledger["positive_confirmation_years"].ge(int(statistics["minimum_positive_confirmation_years"]))
        & ledger["positive_horizon_count"].ge(int(statistics["minimum_positive_horizons"]))
        & ledger["bootstrap_positive_probability"].ge(float(statistics["minimum_bootstrap_positive_probability"]))
        & ledger["etf_top_minus_bottom_return"].gt(0)
    )
    ledger["evidence_label"] = "NO_STABLE_EVIDENCE"
    ledger.loc[~ledger["identifiable"], "evidence_label"] = "UNIDENTIFIABLE"
    ledger.loc[stable, "evidence_label"] = "DIRECTIONALLY_STABLE"
    ledger.loc[stable & ledger["hac_one_sided_pvalue"].le(float(statistics["nominal_one_sided_alpha"])), "evidence_label"] = "NOMINAL_SUPPORT"
    ledger.loc[stable & ledger["global_bh_qvalue"].le(float(statistics["benjamini_hochberg_fdr"])), "evidence_label"] = "FDR_SUPPORTED"
    ledger = ledger.sort_values(["horizon_sessions", "group", "confirmation_etf_ic"], ascending=[False, True, False]).reset_index(drop=True)

    supported_labels = {"DIRECTIONALLY_STABLE", "NOMINAL_SUPPORT", "FDR_SUPPORTED"}
    primary = ledger.loc[ledger["horizon_sessions"].eq(primary_horizon)]
    supported = primary.loc[primary["evidence_label"].isin(supported_labels)]
    upside_count = int(supported["group"].eq("CORE_UPSIDE").sum())
    complement_count = int(supported["group"].eq("RECOVERY_OR_RISK").sum())
    if upside_count == 0:
        decision = protocol["adjudication"]["no_upside"]
    elif complement_count == 0:
        decision = protocol["adjudication"]["no_complement"]
    else:
        decision = protocol["adjudication"]["all_pass"]

    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    ledger.to_csv(artifacts / "cross_era_path_ledger.csv.gz", index=False, compression=compression, lineterminator="\n")
    supported.to_csv(artifacts / "primary_supported_paths.csv", index=False, encoding="utf-8", lineterminator="\n")
    family_summary = ledger.groupby(["group", "family", "horizon_sessions"], as_index=False).agg(
        paths=("path_id", "size"),
        median_discovery_sge_ic=("discovery_sge_ic", "median"),
        median_confirmation_sge_ic=("confirmation_sge_ic", "median"),
        median_confirmation_etf_ic=("confirmation_etf_ic", "median"),
        supported_paths=("evidence_label", lambda values: int(values.isin(supported_labels).sum())),
    )
    family_summary.to_csv(artifacts / "family_horizon_summary.csv", index=False, encoding="utf-8", lineterminator="\n")
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "decision": decision,
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "dataset_identities": {key: {"dataset": result.identity.dataset, "source": result.identity.source, "data_start": result.identity.data_start, "data_cutoff": result.identity.data_cutoff, "content_sha256": result.identity.content_sha256, "rows": len(result.dataframe)} for key, result in results.items()},
        "data_gate_pass": data_gate_pass,
        "feature_count": len(feature_specs),
        "path_count": len(ledger),
        "primary_supported_count": len(supported),
        "primary_core_upside_count": upside_count,
        "primary_recovery_or_risk_count": complement_count,
        "evidence_label_counts": dict(sorted(Counter(ledger["evidence_label"]).items())),
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
    _write(artifacts / "cross_era_information_audit.json", summary)
    (experiment / "03_execution.md").write_text(
        "# S008 EX50 执行记录\n\n"
        f"13项预注册特征在120/252日形成{len(ledger)}条路径。方向由金融机制预先固定；"
        "全部路径同时计算上市前SGE、上市后SGE和上市后ETF证据。未读取密封池、选择策略输入、"
        "实例化原型或启动搜索。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX50 结论\n\n"
        f"机器裁决：`{decision}`。252日主周期支持路径={len(supported)}，其中上涨持有来源="
        f"{upside_count}，恢复或风险补充={complement_count}。本实验只授予或拒绝第七原型家族的"
        "设计资格，不构成策略、参数或候选证据。\n",
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
