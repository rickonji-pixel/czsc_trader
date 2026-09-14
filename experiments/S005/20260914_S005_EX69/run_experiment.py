from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.identity import raw_file_sha256
from czsc_trader.intraday_data import load_intraday_research_data


EXPERIMENT_ID = "20260914_S005_EX69"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _causal_percentile(
    values: pd.Series,
    window: int,
    minimum: int,
) -> pd.Series:
    def rank_last(items: np.ndarray) -> float:
        current = items[-1]
        valid = items[np.isfinite(items)]
        if not np.isfinite(current) or len(valid) < minimum:
            return np.nan
        return float(
            (np.count_nonzero(valid < current) + 0.5 * np.count_nonzero(valid == current))
            / len(valid)
        )

    return values.astype(float).rolling(window, min_periods=minimum).apply(rank_last, raw=True)


def _spearman(left: pd.Series, right: pd.Series) -> float:
    frame = pd.concat([left, right], axis=1, sort=False).dropna()
    if len(frame) < 3:
        return float("nan")
    return float(frame.iloc[:, 0].corr(frame.iloc[:, 1], method="spearman"))


def _bootstrap(
    frame: pd.DataFrame,
    block: int,
    samples: int,
    seed: int,
    high: float,
    low: float,
) -> tuple[float, float]:
    score = frame["score"].rank(method="average").to_numpy(dtype=float)
    returns = frame["stress_return"].rank(method="average").to_numpy(dtype=float)
    raw_returns = frame["stress_return"].to_numpy(dtype=float)
    percentiles = frame["score"].to_numpy(dtype=float)
    length = len(frame)
    if length < block * 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    ic_positive = 0
    spread_positive = 0
    valid_ic = 0
    valid_spread = 0
    block_count = int(np.ceil(length / block))
    for _ in range(samples):
        starts = rng.integers(0, length - block + 1, size=block_count)
        indices = np.concatenate(
            [np.arange(start, start + block, dtype=int) for start in starts]
        )[:length]
        sampled_score = score[indices]
        sampled_return = returns[indices]
        if sampled_score.std() > 0 and sampled_return.std() > 0:
            correlation = float(np.corrcoef(sampled_score, sampled_return)[0, 1])
            ic_positive += int(correlation > 0)
            valid_ic += 1
        sampled_percentiles = percentiles[indices]
        high_values = raw_returns[indices][sampled_percentiles >= high]
        low_values = raw_returns[indices][sampled_percentiles <= low]
        if len(high_values) and len(low_values):
            spread_positive += int(float(high_values.mean() - low_values.mean()) > 0)
            valid_spread += 1
    return (
        float(ic_positive / valid_ic) if valid_ic else float("nan"),
        float(spread_positive / valid_spread) if valid_spread else float("nan"),
    )


def _factor_label(metrics: dict[str, object], protocol: dict[str, object]) -> str:
    robustness = protocol["robustness"]
    complementarity = protocol["complementarity"]
    ic = float(metrics["spearman_ic"])
    spread = float(metrics["top_bottom_spread"])
    residual_ic = float(metrics["residual_spearman_ic"])
    max_corr = float(metrics["maximum_absolute_reference_correlation"])
    if max_corr >= float(complementarity["redundancy_absolute_correlation"]) and residual_ic <= 0:
        return "REDUNDANT"
    if ic <= 0 and spread <= 0:
        return "HARMFUL"
    supportive = all(
        (
            ic > 0,
            float(metrics["top_stress_mean"]) > 0,
            spread > 0,
            residual_ic > 0,
            int(metrics["positive_year_ics"]) >= int(robustness["minimum_positive_years"]),
            int(metrics["positive_leave_one_year_out_ics"])
            >= int(robustness["minimum_positive_leave_one_year_out"]),
            float(metrics["bootstrap_ic_positive_probability"])
            >= float(robustness["minimum_positive_probability"]),
            float(metrics["bootstrap_spread_positive_probability"])
            >= float(robustness["minimum_positive_probability"]),
            max_corr < float(complementarity["redundancy_absolute_correlation"]),
        )
    )
    return "SUPPORTIVE" if supportive else "REGIME_DEPENDENT"


def _source(
    repo: Path,
    spec: dict[str, object],
    artifact_name: str,
    artifact_hash_key: str,
) -> Path:
    source = repo / "experiments/S005" / str(spec["experiment_id"])
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": spec["manifest_sha256"],
        source / artifact_name: spec[artifact_hash_key],
    }
    for path, digest in expected.items():
        if raw_file_sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path}")
    return source


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("component_frequency_policy") != "REPORT_ONLY":
        raise ValueError("component frequency must be report-only")

    sources = protocol["sources"]
    correction = _source(
        repo,
        sources["correction"],
        "artifacts/correction_evidence.json",
        "evidence_sha256",
    )
    correction_evidence = _read(correction / "artifacts/correction_evidence.json")
    if correction_evidence.get("decision") != "PROCEED_TO_CONTINUOUS_COMPONENT_INFORMATION_AUDIT":
        raise ValueError("EX68 did not authorize continuous component audit")
    industry_source = _source(
        repo,
        sources["industry"],
        "artifacts/external_industry_flow_ledger.csv.gz",
        "ledger_sha256",
    )
    expectation_source = _source(
        repo,
        sources["expectation"],
        "artifacts/expectation_revision_ledger.csv.gz",
        "ledger_sha256",
    )
    reference_source = _source(
        repo,
        sources["reference_factors"],
        "artifacts/factor_observation_ledger.csv.gz",
        "ledger_sha256",
    )
    manifest_path = repo / "data/raw/588080_intraday_manifest.json"
    if raw_file_sha256(manifest_path) != sources["intraday_manifest_sha256"]:
        raise ValueError("intraday research generation differs from frozen protocol")

    intraday = load_intraday_research_data(repo / "data/raw", str(protocol["symbol"]))
    bars = intraday.frames["1m"].copy()
    bars["date"] = bars["Date"].dt.normalize()
    bars["clock"] = bars["Date"].dt.strftime("%H:%M")
    cutoff = pd.Timestamp(str(protocol["development_cutoff"]))
    bars = bars.loc[bars["date"].le(cutoff)]
    if not bars.groupby("date").size().eq(240).all():
        raise ValueError("incomplete minute sessions")
    calendar = pd.DatetimeIndex(sorted(bars["date"].unique()))
    positions = {session: index for index, session in enumerate(calendar)}
    execution = protocol["execution"]
    entry_prices = bars.loc[bars["clock"].eq(str(execution["entry_clock"]))].set_index("date")["Open"].astype(float)
    exit_prices = bars.loc[bars["clock"].eq(str(execution["exit_clock"]))].set_index("date")["Open"].astype(float)
    closes = bars.groupby("date", sort=True)["Close"].last().astype(float)

    forward_rows: list[dict[str, object]] = []
    cost = float(execution["stress_one_way_cost"])
    for signal_date, position in positions.items():
        entry_index = position + int(execution["entry_offset_sessions"])
        exit_index = position + int(execution["exit_offset_sessions"])
        if exit_index >= len(calendar):
            continue
        entry_date = calendar[entry_index]
        exit_date = calendar[exit_index]
        entry = float(entry_prices.loc[entry_date])
        exit_ = float(exit_prices.loc[exit_date])
        forward_rows.append(
            {
                "trade_date": signal_date,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "stress_return": exit_ * (1.0 - cost) / (entry * (1.0 + cost)) - 1.0,
            }
        )
    forward = pd.DataFrame(forward_rows).set_index("trade_date")
    normalization = protocol["factor_normalization"]
    window = int(normalization["reference_sessions"])
    minimum = int(normalization["minimum_observations"])

    industry = pd.read_csv(
        industry_source / "artifacts/external_industry_flow_ledger.csv.gz",
        usecols=["trade_date", "positive_member_ratio", "net_flow_ratio"],
        parse_dates=["trade_date"],
    ).set_index("trade_date").sort_index()
    industry_breadth = _causal_percentile(
        industry["positive_member_ratio"], window, minimum
    )
    industry_flow = _causal_percentile(industry["net_flow_ratio"], window, minimum)
    industry_raw = pd.concat([industry_breadth, industry_flow], axis=1).mean(
        axis=1, skipna=False
    )
    industry_score = _causal_percentile(industry_raw, window, minimum)

    expectation = pd.read_csv(
        expectation_source / "artifacts/expectation_revision_ledger.csv.gz",
        usecols=["trade_date", "revision_score"],
        parse_dates=["trade_date"],
    ).set_index("trade_date").sort_index()
    expectation_score = _causal_percentile(
        expectation["revision_score"], window, minimum
    )
    factor_scores = {
        "F-PROJECT-EXTERNAL-INDUSTRY-MONEYFLOW": industry_score,
        "F-PROJECT-SELL-SIDE-REVISION-BREADTH": expectation_score,
    }

    controls = protocol["controls"]
    close = closes.reindex(calendar)
    past_return = close.pct_change(int(controls["past_return_sessions"]), fill_method=None)
    volatility = np.log(close).diff().rolling(int(controls["volatility_sessions"])).std()
    past_percentile = _causal_percentile(past_return, window, minimum)
    volatility_percentile = _causal_percentile(volatility, window, minimum)

    reference = pd.read_csv(
        reference_source / "artifacts/factor_observation_ledger.csv.gz",
        usecols=["factor_id", "first_usable_date", "first_usable_clock", "value_numeric"],
        parse_dates=["first_usable_date"],
    )
    reference = reference.loc[
        reference["value_numeric"].notna()
        & reference["first_usable_clock"].astype(str).le(
            str(protocol["complementarity"]["reference_latest_clock"])
        )
    ]
    reference_wide = reference.pivot_table(
        index="first_usable_date",
        columns="factor_id",
        values="value_numeric",
        aggfunc="last",
    )

    summary_rows: list[dict[str, object]] = []
    annual_rows: list[dict[str, object]] = []
    correlation_rows: list[dict[str, object]] = []
    observation_rows: list[pd.DataFrame] = []
    high = float(normalization["high_percentile"])
    low = float(normalization["low_percentile"])
    for factor_id, score in factor_scores.items():
        frame = forward.join(score.rename("score"), how="inner")
        frame["past_return_percentile"] = past_percentile.reindex(frame.index)
        frame["volatility_percentile"] = volatility_percentile.reindex(frame.index)
        frame = frame.dropna(
            subset=["score", "stress_return", "past_return_percentile", "volatility_percentile"]
        ).sort_index()
        frame["year"] = frame.index.year
        frame["past_bucket"] = np.minimum(
            (frame["past_return_percentile"] * int(controls["past_return_buckets"])).astype(int),
            int(controls["past_return_buckets"]) - 1,
        )
        frame["volatility_bucket"] = np.minimum(
            (frame["volatility_percentile"] * int(controls["volatility_buckets"])).astype(int),
            int(controls["volatility_buckets"]) - 1,
        )
        frame["matched_mean_return"] = frame.groupby(
            ["year", "past_bucket", "volatility_bucket"], observed=True
        )["stress_return"].transform("mean")
        frame["residual_return"] = frame["stress_return"] - frame["matched_mean_return"]
        frame["factor_id"] = factor_id
        observation_rows.append(frame.reset_index())

        high_values = frame.loc[frame["score"].ge(high), "stress_return"]
        low_values = frame.loc[frame["score"].le(low), "stress_return"]
        year_ics: dict[int, float] = {}
        for year, group in frame.groupby("year", observed=True):
            if len(group) < int(protocol["robustness"]["minimum_year_observations"]):
                continue
            year_ics[int(year)] = _spearman(group["score"], group["stress_return"])
            annual_rows.append(
                {
                    "factor_id": factor_id,
                    "year": int(year),
                    "observations": int(len(group)),
                    "spearman_ic": year_ics[int(year)],
                    "top_stress_mean": float(
                        group.loc[group["score"].ge(high), "stress_return"].mean()
                    ),
                }
            )
        leave_one_out = {
            year: _spearman(
                frame.loc[frame["year"].ne(year), "score"],
                frame.loc[frame["year"].ne(year), "stress_return"],
            )
            for year in sorted(frame["year"].unique())
        }
        ic_probability, spread_probability = _bootstrap(
            frame,
            int(protocol["robustness"]["bootstrap_block_sessions"]),
            int(protocol["robustness"]["bootstrap_samples"]),
            int(protocol["robustness"]["bootstrap_seed"]),
            high,
            low,
        )

        correlations: dict[str, float] = {
            "TARGET_PAST_3D_RETURN": _spearman(score, past_return),
            "TARGET_20D_VOLATILITY": _spearman(score, volatility),
        }
        for reference_id in reference_wide.columns:
            correlations[str(reference_id)] = _spearman(
                score, reference_wide[reference_id]
            )
        for other_id, other_score in factor_scores.items():
            if other_id != factor_id:
                correlations[other_id] = _spearman(score, other_score)
        valid_correlations = {
            key: value
            for key, value in correlations.items()
            if np.isfinite(value)
        }
        for reference_id, correlation in sorted(valid_correlations.items()):
            correlation_rows.append(
                {
                    "factor_id": factor_id,
                    "reference_id": reference_id,
                    "spearman_correlation": correlation,
                }
            )
        max_reference = max(
            valid_correlations,
            key=lambda key: abs(valid_correlations[key]),
        )
        high_state = frame["score"].ge(high)
        rolling_high = high_state.astype(int).rolling(60, min_periods=60).sum().dropna()
        metrics: dict[str, object] = {
            "factor_id": factor_id,
            "observations": int(len(frame)),
            "spearman_ic": _spearman(frame["score"], frame["stress_return"]),
            "top_observations": int(len(high_values)),
            "top_stress_mean": float(high_values.mean()),
            "bottom_observations": int(len(low_values)),
            "bottom_stress_mean": float(low_values.mean()),
            "top_bottom_spread": float(high_values.mean() - low_values.mean()),
            "residual_spearman_ic": _spearman(frame["score"], frame["residual_return"]),
            "positive_year_ics": int(sum(value > 0 for value in year_ics.values())),
            "observed_years": int(len(year_ics)),
            "positive_leave_one_year_out_ics": int(
                sum(value > 0 for value in leave_one_out.values())
            ),
            "leave_one_year_out_tests": int(len(leave_one_out)),
            "bootstrap_ic_positive_probability": ic_probability,
            "bootstrap_spread_positive_probability": spread_probability,
            "maximum_absolute_reference_correlation": float(
                abs(valid_correlations[max_reference])
            ),
            "maximum_correlation_reference": max_reference,
            "high_state_days": int(high_state.sum()),
            "high_state_entries": int((high_state & ~high_state.shift(1, fill_value=False)).sum()),
            "rolling_60_high_days_median": float(rolling_high.median()),
            "rolling_60_high_days_p10": float(rolling_high.quantile(0.10)),
            "frequency_policy": "REPORT_ONLY",
        }
        metrics["label"] = _factor_label(metrics, protocol)
        summary_rows.append(metrics)

    summary = pd.DataFrame(summary_rows)
    annual = pd.DataFrame(annual_rows)
    correlations = pd.DataFrame(correlation_rows)
    observations = pd.concat(observation_rows, ignore_index=True)
    labels = dict(zip(summary["factor_id"], summary["label"], strict=True))
    decision = (
        "PROCEED_TO_COMPONENT_ROLE_AND_COMBINATION_DESIGN"
        if any(label in {"SUPPORTIVE", "REGIME_DEPENDENT"} for label in labels.values())
        else "STOP_BOTH_INCREMENTAL_COMPONENTS_ON_INFORMATION_VALUE"
    )
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "labels": labels,
        "decision": decision,
        "prior_cumulative_return_path_count": protocol["prior_cumulative_return_path_count"],
        "component_return_path_count": protocol["component_return_path_count"],
        "cumulative_return_path_count": protocol["cumulative_return_path_count"],
        "component_frequency_policy": protocol["component_frequency_policy"],
        "complete_strategy_frequency_policy": protocol["complete_strategy_frequency_policy"],
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    summary.to_csv(artifacts / "component_summary.csv", index=False, lineterminator="\n")
    annual.to_csv(artifacts / "annual_metrics.csv", index=False, lineterminator="\n")
    correlations.to_csv(
        artifacts / "complementarity.csv", index=False, lineterminator="\n"
    )
    observations.to_csv(
        artifacts / "observation_ledger.csv.gz",
        index=False,
        compression=compression,
        lineterminator="\n",
        date_format="%Y-%m-%d",
    )
    (artifacts / "information_audit.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    rows = [
        "|组件|IC|最高组压力均值|最高-最低|残差IC|正IC年度|Bootstrap IC/差值|最大相关|标签|",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in summary.itertuples(index=False):
        rows.append(
            f"|{row.factor_id}|{row.spearman_ic:.3f}|{row.top_stress_mean:.3%}|"
            f"{row.top_bottom_spread:.3%}|{row.residual_spearman_ic:.3f}|"
            f"{row.positive_year_ics}/{row.observed_years}|"
            f"{row.bootstrap_ic_positive_probability:.1%}/{row.bootstrap_spread_positive_probability:.1%}|"
            f"{row.maximum_absolute_reference_correlation:.3f}|{row.label}|"
        )
    (experiment / "03_execution.md").write_text(
        "# S005 EX69 执行\n\n状态：`COMPLETE`。两项连续组件按固定三日收益口径完成信息、稳健性与互补性审计。"
        "组件频率只披露，逐日观察允许重叠且不解释为账户收益。\n\n"
        + "\n".join(rows)
        + "\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX69 结论\n\n"
        f"裁决：`{decision}`。组件标签："
        + "；".join(f"`{factor}`={label}" for factor, label in labels.items())
        + f"。累计收益研究路径由{protocol['prior_cumulative_return_path_count']}增至"
        f"{protocol['cumulative_return_path_count']}。本轮没有形成完整策略、候选、SM或PTE变更。\n",
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
            "candidate_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
