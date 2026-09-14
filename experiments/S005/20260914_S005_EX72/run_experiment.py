from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd
import statsmodels.api as sm

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S005_EX72"


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
    frame = pd.concat([left.rename("left"), right.rename("right")], axis=1, sort=False).dropna()
    if len(frame) < 3 or frame["left"].nunique() < 2 or frame["right"].nunique() < 2:
        return None
    return float(frame["left"].corr(frame["right"], method="spearman"))


def _hac_one_sided_pvalue(score: pd.Series, outcome: pd.Series, max_lags: int) -> tuple[float | None, float | None]:
    frame = pd.concat([score.rename("score"), outcome.rename("outcome")], axis=1, sort=False).dropna()
    if len(frame) < 20 or frame["score"].nunique() < 2:
        return None, None
    normalized = (frame["score"] - frame["score"].mean()) / frame["score"].std(ddof=0)
    fitted = sm.OLS(frame["outcome"].to_numpy(dtype=float), sm.add_constant(normalized.to_numpy(dtype=float))).fit(
        cov_type="HAC", cov_kwds={"maxlags": int(max_lags)}
    )
    coefficient = float(fitted.params[1])
    two_sided = float(fitted.pvalues[1])
    one_sided = two_sided / 2.0 if coefficient >= 0 else 1.0 - two_sided / 2.0
    return coefficient, one_sided


def _bh_qvalues(values: pd.Series) -> pd.Series:
    result = pd.Series(np.nan, index=values.index, dtype=float)
    valid = values.dropna().sort_values()
    if valid.empty:
        return result
    count = len(valid)
    ranks = np.arange(1, count + 1, dtype=float)
    adjusted = valid.to_numpy(dtype=float) * count / ranks
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    result.loc[valid.index] = np.minimum(adjusted, 1.0)
    return result


def _residualize(outcome: pd.Series, prices: pd.DataFrame, protocol: dict[str, object]) -> pd.Series:
    controls = protocol["controls"]
    close = prices["close"].astype(float)
    past = close.pct_change(int(controls["past_return_sessions"])).reindex(outcome.index)
    volatility = (
        close.pct_change().rolling(int(controls["volatility_sessions"])).std().reindex(outcome.index)
    )
    years = pd.get_dummies(outcome.index.year, prefix="year", drop_first=True, dtype=float)
    years.index = outcome.index
    frame = pd.concat(
        [outcome.rename("outcome"), past.rename("past_return"), volatility.rename("volatility"), years],
        axis=1,
    ).dropna()
    regressors = sm.add_constant(frame.drop(columns="outcome").astype(float))
    fitted = sm.OLS(frame["outcome"].astype(float), regressors).fit()
    residual = pd.Series(np.nan, index=outcome.index, dtype=float)
    residual.loc[frame.index] = fitted.resid
    return residual


def _frequency_descriptor(states: pd.Series) -> tuple[int, float, float]:
    valid = states.notna()
    changes = valid & states.ne(states.shift(1))
    rolling = changes.astype(int).rolling(60, min_periods=60).sum().dropna()
    return (
        int(changes.sum()),
        float(rolling.median()) if not rolling.empty else 0.0,
        float(rolling.quantile(0.1)) if not rolling.empty else 0.0,
    )


def main() -> None:
    started = time.perf_counter()
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX72 cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    ex71 = repo / "experiments" / "S005" / str(sources["eligibility_experiment_id"])
    ex49 = repo / "experiments" / "S005" / str(sources["state_matrix_experiment_id"])
    ex69 = repo / "experiments" / "S005" / str(sources["industry_experiment_id"])
    for source in (ex71, ex49, ex69):
        validate_experiment_archive(source)
    frozen_files = (
        (ex71 / "experiment_manifest.json", sources["eligibility_manifest_sha256"]),
        (ex71 / "artifacts/reclassified_state_ledger.csv.gz", sources["eligibility_ledger_sha256"]),
        (ex49 / "artifacts/primary_states.csv.gz", sources["primary_states_sha256"]),
        (ex69 / "artifacts/observation_ledger.csv.gz", sources["industry_ledger_sha256"]),
    )
    for path, expected in frozen_files:
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
        raise ValueError("research dataset differs from the frozen EX72 protocol")
    prices = replay.adjusted.daily.copy()
    prices["dt"] = pd.to_datetime(prices["dt"]).dt.normalize()
    prices = prices.set_index("dt").sort_index()
    prices = prices.loc[prices.index <= pd.Timestamp(protocol["development_cutoff"])]
    opens = prices["open"].astype(float)
    hold = int(protocol["outcome"]["forward_open_intervals"])
    forward_return = opens.shift(-(hold + 1)).div(opens.shift(-1)).sub(1.0)

    primary = pd.read_csv(ex49 / "artifacts/primary_states.csv.gz")
    primary["dt"] = pd.to_datetime(primary["dt"]).dt.normalize()
    primary = primary.set_index("dt").reindex(prices.index)
    ledger = pd.read_csv(ex71 / "artifacts/reclassified_state_ledger.csv.gz")
    ledger = ledger.loc[ledger["technical_eligible_v2"].astype(bool)].copy()
    if ledger["signal_id"].nunique() != int(protocol["return_path_count"]):
        raise ValueError("signal path count differs from frozen protocol")

    discovery_mask = (
        (prices.index >= pd.Timestamp(protocol["dataset"]["discovery_start"]))
        & (prices.index <= pd.Timestamp(protocol["dataset"]["discovery_end"]))
        & forward_return.notna().to_numpy()
    )
    confirmation_mask = (
        (prices.index >= pd.Timestamp(protocol["dataset"]["confirmation_start"]))
        & (prices.index <= pd.Timestamp(protocol["dataset"]["confirmation_end"]))
        & forward_return.notna().to_numpy()
    )
    discovery_outcome = forward_return.loc[discovery_mask]
    confirmation_outcome = forward_return.loc[confirmation_mask]
    confirmation_residual = _residualize(confirmation_outcome, prices, protocol)

    industry = pd.read_csv(ex69 / "artifacts/observation_ledger.csv.gz")
    industry = industry.loc[
        industry["factor_id"].eq("F-PROJECT-EXTERNAL-INDUSTRY-MONEYFLOW")
    ].copy()
    industry["trade_date"] = pd.to_datetime(industry["trade_date"]).dt.normalize()
    industry_score = industry.drop_duplicates("trade_date").set_index("trade_date")["score"].astype(float)

    result_rows: list[dict[str, object]] = []
    state_rows: list[dict[str, object]] = []
    score_columns: dict[str, pd.Series] = {}
    prior_count = float(protocol["model"]["state_shrinkage_prior_count"])
    minimum_scored = int(protocol["model"]["minimum_confirmation_nonzero_score_sessions"])
    annual_minimum = int(protocol["model"]["minimum_annual_sessions"])

    for signal_id, signal_ledger in ledger.groupby("signal_id", sort=True):
        raw_states = primary[str(signal_id)]
        train_states = raw_states.loc[discovery_outcome.index]
        baseline = float(discovery_outcome.mean())
        state_edges: dict[str, float] = {}
        for row in signal_ledger.itertuples(index=False):
            active = train_states.eq(row.state_primary)
            count = int(active.sum())
            raw_edge = float(discovery_outcome.loc[active].mean() - baseline) if count else 0.0
            shrinkage = count / (count + prior_count) if count else 0.0
            edge = raw_edge * shrinkage
            state_edges[str(row.state_primary)] = edge
            state_rows.append({
                "signal_id": signal_id,
                "state_id": row.state_id,
                "state_primary": row.state_primary,
                "information_family": row.information_family,
                "frequency": row.frequency,
                "discovery_occurrences": count,
                "discovery_raw_edge": raw_edge,
                "shrinkage": shrinkage,
                "frozen_state_score": edge,
                "restored_from_old_filter": bool(row.restored_from_old_filter),
            })

        discovery_score = train_states.map(state_edges).fillna(0.0).astype(float)
        confirmation_states = raw_states.loc[confirmation_outcome.index]
        score = confirmation_states.map(state_edges).fillna(0.0).astype(float)
        score_columns[str(signal_id)] = score
        discovery_ic = _spearman(discovery_score, discovery_outcome)
        confirmation_ic = _spearman(score, confirmation_outcome)
        residual_ic = _spearman(score, confirmation_residual)
        nonzero_score_sessions = int(score.ne(0.0).sum())
        identifiable = nonzero_score_sessions >= minimum_scored and score.nunique() >= 2
        if identifiable:
            coefficient, pvalue = _hac_one_sided_pvalue(
                score, confirmation_outcome, int(protocol["statistics"]["hac_max_lags"])
            )
        else:
            coefficient, pvalue = None, None
        annual_ics: dict[str, float | None] = {}
        for year, year_outcome in confirmation_outcome.groupby(confirmation_outcome.index.year):
            year_score = score.loc[year_outcome.index]
            annual_ics[str(int(year))] = (
                _spearman(year_score, year_outcome) if len(year_outcome) >= annual_minimum else None
            )
        positive_years = sum(value is not None and value > 0 for value in annual_ics.values())
        preferred = score > 0
        avoided = score < 0
        preferred_mean = float(confirmation_outcome.loc[preferred].mean()) if preferred.any() else None
        avoided_mean = float(confirmation_outcome.loc[avoided].mean()) if avoided.any() else None
        spread = (
            float(preferred_mean - avoided_mean)
            if preferred_mean is not None and avoided_mean is not None
            else None
        )
        transitions, rolling_median, rolling_p10 = _frequency_descriptor(raw_states)
        industry_correlation = _spearman(score, industry_score)
        result_rows.append({
            "signal_id": signal_id,
            "catalog_signal_id": signal_ledger["catalog_signal_id"].iloc[0],
            "name": signal_ledger["name"].iloc[0],
            "frequency": signal_ledger["frequency"].iloc[0],
            "information_family": signal_ledger["information_family"].iloc[0],
            "state_count": int(len(signal_ledger)),
            "contains_restored_state": bool(signal_ledger["restored_from_old_filter"].any()),
            "s001_reference_function": bool(signal_ledger["s001_reference_function"].any()),
            "discovery_ic": discovery_ic,
            "confirmation_ic": confirmation_ic,
            "confirmation_residual_ic": residual_ic,
            "hac_coefficient": coefficient,
            "hac_one_sided_pvalue": pvalue,
            "positive_confirmation_years": int(positive_years),
            "annual_ics": annual_ics,
            "nonzero_score_sessions": nonzero_score_sessions,
            "preferred_sessions": int(preferred.sum()),
            "avoided_sessions": int(avoided.sum()),
            "preferred_mean_return": preferred_mean,
            "avoided_mean_return": avoided_mean,
            "preferred_minus_avoided": spread,
            "industry_score_correlation": industry_correlation,
            "state_changes": transitions,
            "rolling_60_state_change_median": rolling_median,
            "rolling_60_state_change_p10": rolling_p10,
            "frequency_policy": "REPORT_ONLY",
            "identifiable": bool(identifiable),
        })

    results = pd.DataFrame(result_rows)
    results["bh_qvalue"] = _bh_qvalues(results["hac_one_sided_pvalue"])
    stable = (
        results["identifiable"]
        & results["confirmation_ic"].gt(0)
        & results["confirmation_residual_ic"].gt(0)
        & results["positive_confirmation_years"].ge(
            int(protocol["statistics"]["minimum_positive_confirmation_years"])
        )
    )
    results["evidence_label"] = "NO_STABLE_EVIDENCE"
    results.loc[~results["identifiable"], "evidence_label"] = "UNIDENTIFIABLE"
    results.loc[stable, "evidence_label"] = "DIRECTIONALLY_STABLE"
    results.loc[
        stable & results["hac_one_sided_pvalue"].le(float(protocol["statistics"]["nominal_one_sided_alpha"])),
        "evidence_label",
    ] = "NOMINAL_SUPPORT"
    results.loc[
        stable & results["bh_qvalue"].le(float(protocol["statistics"]["benjamini_hochberg_fdr"])),
        "evidence_label",
    ] = "FDR_SUPPORTED"
    results = results.sort_values(
        ["evidence_label", "bh_qvalue", "confirmation_residual_ic", "confirmation_ic"],
        ascending=[True, True, False, False],
    ).reset_index(drop=True)

    score_matrix = pd.DataFrame(score_columns, index=confirmation_outcome.index)
    score_matrix.index.name = "date"
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    results.to_csv(
        artifacts / "signal_information_ledger.csv.gz",
        index=False,
        encoding="utf-8",
        compression=compression,
        lineterminator="\n",
    )
    pd.DataFrame(state_rows).to_csv(
        artifacts / "frozen_state_scores.csv.gz",
        index=False,
        encoding="utf-8",
        compression=compression,
        lineterminator="\n",
    )
    score_matrix.reset_index().to_csv(
        artifacts / "confirmation_score_matrix.csv.gz",
        index=False,
        encoding="utf-8",
        compression=compression,
        lineterminator="\n",
    )
    family = (
        results.groupby(["information_family", "frequency"], as_index=False)
        .agg(
            signal_paths=("signal_id", "size"),
            restored_paths=("contains_restored_state", "sum"),
            identifiable_paths=("identifiable", "sum"),
            positive_confirmation_ic=("confirmation_ic", lambda values: int(values.gt(0).sum())),
            median_confirmation_ic=("confirmation_ic", "median"),
            median_residual_ic=("confirmation_residual_ic", "median"),
        )
        .sort_values(["information_family", "frequency"])
    )
    family.to_csv(artifacts / "family_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    top = results.loc[
        results["evidence_label"].isin(["FDR_SUPPORTED", "NOMINAL_SUPPORT", "DIRECTIONALLY_STABLE"])
    ].copy()
    top.to_csv(artifacts / "supported_signal_shortlist.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    labels = results["evidence_label"].value_counts().sort_index()
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "dataset_fingerprint": replay.fingerprint,
        "discovery_sessions": int(discovery_mask.sum()),
        "confirmation_sessions": int(confirmation_mask.sum()),
        "signal_path_count": int(len(results)),
        "state_model_count": int(len(state_rows)),
        "restored_signal_path_count": int(results["contains_restored_state"].sum()),
        "evidence_label_counts": {str(key): int(value) for key, value in labels.items()},
        "fdr_supported_count": int(results["evidence_label"].eq("FDR_SUPPORTED").sum()),
        "nominal_support_count": int(results["evidence_label"].eq("NOMINAL_SUPPORT").sum()),
        "directionally_stable_count": int(results["evidence_label"].eq("DIRECTIONALLY_STABLE").sum()),
        "component_frequency_policy": protocol["component_frequency_policy"],
        "complete_strategy_frequency_policy": protocol["complete_strategy_frequency_policy"],
        "prior_cumulative_return_path_count": protocol["prior_cumulative_return_path_count"],
        "cumulative_return_path_count": protocol["cumulative_return_path_count"],
        "search_penalty_note": "all 597 categorical signal paths remain in the later SE search audit",
        "decision": "PROCEED_TO_COMPLEMENTARITY_AND_ARCHITECTURE_DESIGN" if len(top) else "STOP_CZSC_SIGNAL_INFORMATION_AUDIT",
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": _sha256(protocol_path),
    }
    _write(artifacts / "information_audit.json", evidence)

    (experiment / "03_execution.md").write_text(
        "# S005 EX72 执行\n\n"
        f"状态：`COMPLETE`。{len(results)}个信号配置、{len(state_rows)}个状态完成发现期方向学习和"
        f"确认期验证；发现期{int(discovery_mask.sum())}个交易日，确认期"
        f"{int(confirmation_mask.sum())}个交易日。所有组件频率只报告。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX72 结论\n\n"
        f"裁决：`{evidence['decision']}`。597条信号配置路径中，FDR支持"
        f"{evidence['fdr_supported_count']}条、名义支持{evidence['nominal_support_count']}条、"
        f"方向稳定{evidence['directionally_stable_count']}条；全部路径均计入搜索惩罚，累计收益路径"
        f"增至{protocol['cumulative_return_path_count']}条。\n\n"
        "这些标签表示信号状态结构在后段窗口保留信息，不代表可独立交易。下一轮只允许基于已冻结"
        "确认期分数做去冗余、职责和互补性设计，并检验其与行业资金流调节层的协同；完整策略形成"
        "以后才执行`7/3`频率门和风险收益双标杆。\n",
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
