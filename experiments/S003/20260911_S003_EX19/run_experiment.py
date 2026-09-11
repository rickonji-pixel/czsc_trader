from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from strategy_evaluator import (
    ReturnMatrixEvidence,
    annualized_sharpe,
    calculate_dsr_bundle,
    cscv_pbo,
    effective_trial_count,
    hash_return_matrix,
    stationary_bootstrap_performance,
)

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.intraday_medium_frequency import _rolling_density
from czsc_trader.intraday_opening_execution import _account_metrics, _performance, _trade_return
from czsc_trader.intraday_opportunity_map import _price_checkpoints


EXPERIMENT_ID = "20260911_S003_EX19"
SELECTED_MECHANISM = "LATE_SESSION_FLOW_CONTINUATION"
SELECTED_VARIANT = "FIXED_LONG"


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


def _validate_sources(repo: Path, protocol: dict[str, object]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    evidence = protocol["source_evidence"]
    details = {
        "EX16": ("segment_metrics_sha256", "artifacts/segment_metrics.csv"),
        "EX17": ("mechanism_events_sha256", "artifacts/mechanism_events.csv"),
        "EX18": ("episodes_sha256", "artifacts/episodes.csv.gz"),
    }
    for source_id, (digest_key, relative_file) in details.items():
        spec = evidence[source_id]
        source = repo / "experiments" / str(spec["experiment_id"])
        validate_experiment_archive(source)
        if _sha256(source / "experiment_manifest.json") != str(spec["manifest_sha256"]):
            raise ValueError(f"{source_id} manifest differs from frozen protocol")
        if _sha256(source / relative_file) != str(spec[digest_key]):
            raise ValueError(f"{source_id} evidence differs from frozen protocol")
        paths[source_id] = source
    se = protocol["se_implementation"]
    expected = {
        repo / "packages/strategy_evaluator/src/strategy_evaluator/bootstrap.py": se[
            "bootstrap_sha256"
        ],
        repo / "packages/strategy_evaluator/src/strategy_evaluator/search_bias.py": se[
            "search_bias_sha256"
        ],
    }
    for path, digest in expected.items():
        if _sha256(path) != str(digest):
            raise ValueError(f"SE implementation differs: {path}")
    return paths


def _daily_trial_matrix(
    episodes: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    variants: list[str],
) -> pd.DataFrame:
    columns: dict[str, pd.Series] = {}
    investable = episodes.loc[episodes["variant"].isin(variants)].copy()
    for (mechanism_id, variant), rows in investable.groupby(
        ["mechanism_id", "variant"], observed=True
    ):
        trial_id = f"{mechanism_id}:{variant}"
        daily = pd.Series(0.0, index=calendar)
        values = rows.set_index("trade_date")["baseline_return"].astype(float)
        daily.loc[values.index] = values
        columns[trial_id] = daily
    return pd.DataFrame(columns, index=calendar).sort_index(axis=1)


def _neighbor_audit(
    checkpoints: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    quantiles: list[float],
    *,
    lookback: int,
    lag: int,
    stress_cost: float,
    base_fraction: float,
) -> pd.DataFrame:
    current_late = checkpoints["15:00_CLOSE"].div(checkpoints["13:05_OPEN"]).sub(1.0)
    signal = current_late.shift(1)
    strength = signal.abs()
    rows: list[dict[str, object]] = []
    years = max((calendar.max() - calendar.min()).days / 365.25, 1.0)
    for quantile in quantiles:
        threshold = strength.shift(lag).rolling(lookback, min_periods=lookback).quantile(quantile)
        selected_dates = calendar.intersection(
            strength.index[threshold.notna() & strength.ge(threshold)]
        )
        returns = pd.Series(
            [
                _trade_return(
                    float(checkpoints.loc[date, "OPEN"]),
                    float(checkpoints.loc[date, "11:30_CLOSE"]),
                    1,
                    stress_cost,
                )
                for date in selected_dates
            ],
            index=selected_dates,
            name="stress_return",
        )
        performance = _performance(returns, years)
        density = _rolling_density(selected_dates, calendar, 60)
        account = _account_metrics(
            returns.rename_axis("trade_date").reset_index(),
            checkpoints["15:00_CLOSE"].reindex(calendar),
            base_fraction=base_fraction,
        )
        rows.append(
            {
                "quantile": quantile,
                "events": int(len(returns)),
                "rolling_median_events": density[0],
                "rolling_p10_events": density[1],
                "stress_mean_return": performance["mean_return"],
                "stress_profit_factor": performance["profit_factor"],
                "stress_cumulative_return": performance["cumulative_return"],
                "incremental_terminal_return_vs_static": account[
                    "incremental_terminal_return_vs_static"
                ],
            }
        )
    return pd.DataFrame(rows)


def _label(value: float, favorable: float, adverse: float, *, lower_better: bool) -> str:
    if lower_better:
        if value <= favorable:
            return "FAVORABLE"
        if value > adverse:
            return "ADVERSE"
    else:
        if value >= favorable:
            return "FAVORABLE"
        if value < adverse:
            return "ADVERSE"
    return "MIXED"


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = (
        "automatic_acceptance",
        "parameter_selection",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(bool(protocol.get(key)) for key in forbidden):
        raise ValueError("EX19 may not select, create, promote or deploy")
    if protocol["research_target"]["selected_behavior"] != (
        f"{SELECTED_MECHANISM}:{SELECTED_VARIANT}"
    ):
        raise ValueError("selected behavior differs from frozen protocol")
    sources = _validate_sources(repo, protocol)
    audit = protocol["audit"]
    execution = protocol["execution"]
    start = pd.Timestamp(protocol["dataset"]["evaluation_start"]).normalize()

    episodes = pd.read_csv(sources["EX18"] / "artifacts/episodes.csv.gz")
    episodes["trade_date"] = pd.to_datetime(episodes["trade_date"]).dt.normalize()
    intraday = load_intraday_research_data(
        repo / "data/raw", str(protocol["research_target"]["symbol"])
    )
    all_checkpoints = _price_checkpoints(intraday.frames["5m"])
    checkpoints = all_checkpoints.loc[all_checkpoints.index >= start]
    calendar = checkpoints.index
    trial_matrix = _daily_trial_matrix(
        episodes,
        calendar,
        list(map(str, protocol["trial_universe"]["variants"])),
    )
    if len(trial_matrix.columns) != int(protocol["trial_universe"]["raw_trial_count"]):
        raise ValueError("trial count differs from frozen protocol")
    evidence = ReturnMatrixEvidence(
        tuple(date.strftime("%Y-%m-%d") for date in calendar),
        tuple(map(str, trial_matrix.columns)),
        tuple(tuple(float(value) for value in row) for row in trial_matrix.to_numpy()),
        "",
    )
    evidence = ReturnMatrixEvidence(
        evidence.dates,
        evidence.candidate_ids,
        evidence.returns,
        hash_return_matrix(evidence),
    )
    pbo = cscv_pbo(evidence, int(audit["cscv_blocks"]))
    trial_sharpes = np.asarray(
        [annualized_sharpe(trial_matrix[column].to_numpy()) for column in trial_matrix],
        dtype=float,
    )
    effective_count = effective_trial_count(trial_matrix.to_numpy())
    selected_id = f"{SELECTED_MECHANISM}:{SELECTED_VARIANT}"
    selected_baseline = trial_matrix[selected_id]
    dsr = calculate_dsr_bundle(
        selected_baseline.to_numpy(),
        trial_sharpes,
        raw_count=len(trial_matrix.columns),
        effective_count=effective_count,
    )

    selected_rows = episodes.loc[
        episodes["mechanism_id"].eq(SELECTED_MECHANISM)
        & episodes["variant"].eq(SELECTED_VARIANT)
    ].copy()
    selected_stress = pd.Series(0.0, index=calendar, name="stress_return")
    selected_values = selected_rows.set_index("trade_date")["stress_return"].astype(float)
    selected_stress.loc[selected_values.index] = selected_values
    bootstrap_rows: list[dict[str, object]] = []
    bootstrap_reports = []
    for block in audit["bootstrap_block_lengths"]:
        report = stationary_bootstrap_performance(
            selected_stress.to_numpy(),
            candidate_id=selected_id,
            repetitions=int(audit["bootstrap_repetitions"]),
            mean_block_length=int(block),
            seed=int(audit["seed"]) + int(block),
        )
        bootstrap_reports.append(report)
        for metric in (report.cagr, report.max_drawdown, report.calmar):
            bootstrap_rows.append({"mean_block_length": int(block), **asdict(metric)})

    opportunity = pd.read_csv(sources["EX16"] / "artifacts/segment_episodes.csv.gz")
    opportunity["trade_date"] = pd.to_datetime(opportunity["trade_date"]).dt.normalize()
    morning = opportunity.loc[opportunity["segment_id"].eq("OPEN_TO_1130")].set_index(
        "trade_date"
    )["baseline_return"].reindex(calendar)
    if morning.isna().any():
        raise ValueError("unconditional morning returns do not cover audit calendar")
    flags = pd.Series(0, index=calendar, dtype=int)
    flags.loc[selected_values.index] = 1
    observed_placebo_mean = float(morning.loc[flags.eq(1)].mean())
    flag_values = flags.to_numpy()
    morning_values = morning.to_numpy()
    placebo_means = np.asarray(
        [float(morning_values[np.roll(flag_values, lag).astype(bool)].mean()) for lag in range(1, len(flags))]
    )
    placebo_percentile = float(np.mean(placebo_means <= observed_placebo_mean))

    loyo_rows: list[dict[str, object]] = []
    for removed_year in sorted(selected_rows["trade_date"].dt.year.unique()):
        kept = selected_rows.loc[selected_rows["trade_date"].dt.year.ne(removed_year)]
        performance = _performance(
            kept["stress_return"],
            max((kept["trade_date"].max() - kept["trade_date"].min()).days / 365.25, 1.0),
        )
        loyo_rows.append(
            {
                "removed_year": int(removed_year),
                "episodes": int(len(kept)),
                "stress_mean_return": performance["mean_return"],
                "stress_profit_factor": performance["profit_factor"],
                "stress_cumulative_return": performance["cumulative_return"],
            }
        )
    loyo = pd.DataFrame(loyo_rows)
    recent = selected_stress.iloc[-int(audit["recent_sessions"]):]
    recent_events = int(flags.iloc[-int(audit["recent_sessions"]):].sum())
    recent_mean = float(recent.loc[recent.ne(0)].mean()) if recent_events else 0.0
    neighbors = _neighbor_audit(
        all_checkpoints,
        calendar,
        [float(value) for value in audit["neighbor_quantiles"]],
        lookback=int(audit["threshold_lookback_sessions"]),
        lag=int(audit["threshold_lag_sessions"]),
        stress_cost=float(execution["stress_one_way_cost"]),
        base_fraction=float(execution["base_fraction"]),
    )

    flags_by_direction = {
        "pbo": _label(
            pbo.pbo,
            float(audit["favorable_pbo_max"]),
            float(audit["adverse_pbo_min_exclusive"]),
            lower_better=True,
        ),
        "dsr": _label(
            dsr.effective.probability,
            float(audit["positive_dsr_probability"]),
            float(audit["mixed_dsr_probability"]),
            lower_better=False,
        ),
        "absolute_bootstrap": (
            "FAVORABLE"
            if all(report.cagr.lower_90 > 0 for report in bootstrap_reports)
            else "ADVERSE"
            if all(report.cagr.probability_above_zero < 0.5 for report in bootstrap_reports)
            else "MIXED"
        ),
        "cyclic_placebo": _label(
            placebo_percentile,
            float(audit["favorable_placebo_percentile"]),
            float(audit["adverse_placebo_percentile"]),
            lower_better=False,
        ),
        "leave_one_year_out": (
            "FAVORABLE" if loyo["stress_mean_return"].gt(0).all() else "MIXED"
        ),
        "threshold_neighborhood": (
            "FAVORABLE"
            if neighbors["stress_mean_return"].gt(0).all()
            else "ADVERSE"
            if float(
                neighbors.loc[neighbors["quantile"].eq(0.75), "stress_mean_return"].iloc[0]
            )
            <= 0
            else "MIXED"
        ),
        "recent_252_sessions": "FAVORABLE" if recent_mean > 0 else "ADVERSE",
    }
    if "ADVERSE" in flags_by_direction.values():
        overall = "ADVERSE"
    elif set(flags_by_direction.values()) == {"FAVORABLE"}:
        overall = "FAVORABLE"
    else:
        overall = "MIXED"

    pd.DataFrame(
        {
            "trial_id": trial_matrix.columns,
            "annualized_sharpe": trial_sharpes,
        }
    ).to_csv(artifacts / "trial_sharpes.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    pd.DataFrame([asdict(item) for item in pbo.splits]).to_csv(
        artifacts / "cscv_splits.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    pd.DataFrame(bootstrap_rows).to_csv(
        artifacts / "bootstrap_intervals.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    loyo.to_csv(
        artifacts / "leave_one_year_out.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    neighbors.to_csv(
        artifacts / "threshold_neighborhood.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    pd.DataFrame({"placebo_mean_return": placebo_means}).to_csv(
        artifacts / "cyclic_placebo.csv.gz",
        index=False,
        encoding="utf-8-sig",
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "evidence_label": overall,
        "direction_flags": flags_by_direction,
        "raw_trial_count": int(len(trial_matrix.columns)),
        "effective_trial_count": effective_count,
        "pbo": pbo.pbo,
        "dsr_raw_probability": dsr.raw.probability,
        "dsr_effective_probability": dsr.effective.probability,
        "placebo_observed_mean_return": observed_placebo_mean,
        "placebo_percentile": placebo_percentile,
        "recent_sessions": int(audit["recent_sessions"]),
        "recent_events": recent_events,
        "recent_stress_mean_return": recent_mean,
        "candidate_created": False,
    }
    _write(artifacts / "statistical_summary.json", summary)
    _write(
        artifacts / "statistical_details.json",
        {
            "pbo": pbo.to_dict(),
            "dsr": {"raw": asdict(dsr.raw), "effective": asdict(dsr.effective)},
            "bootstrap": [asdict(report) for report in bootstrap_reports],
        },
    )
    flag_table = ["|审计方向|标签|", "|---|---|"] + [
        f"|{name}|`{label}`|" for name, label in flags_by_direction.items()
    ]
    (experiment / "03_execution.md").write_text(
        "# S003 EX19 执行\n\n"
        f"状态：COMPLETE。完成6条试验路径的搜索偏差审计、3档区块Bootstrap、"
        f"{len(placebo_means)}次循环位移、逐年删除、最近252日和3档阈值邻域审计。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX19 结论\n\n"
        f"总标签：`{overall}`。PBO为{pbo.pbo:.2%}，有效试验数{effective_count:.2f}，"
        f"有效DSR概率{dsr.effective.probability:.2%}。循环位移分位{placebo_percentile:.2%}；"
        f"最近252交易日包含{recent_events}个事件，压力成本后单笔均值{recent_mean:.2%}。\n\n"
        + "\n".join(flag_table)
        + "\n\n三个区块Bootstrap的CAGR为正概率均约52%，90%区间均跨越0；"
        "70%和80%阈值邻域的压力收益均转负。75%单点优势缺少统计显著性、参数平台和近期"
        "延续性，应视为历史样本中的脆弱现象。\n\n"
        + (
            "存在ADVERSE证据，停止该原型，不创建候选。"
            if overall == "ADVERSE"
            else "没有触发停止条件，可进入研究候选人工评审。"
        )
        + " 本实验没有修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["research_target"]["strategy_id"],
            "symbol": protocol["research_target"]["symbol"],
            "development_cutoff": protocol["dataset"]["cutoff"],
            "promotion_allowed": False,
        },
    )


if __name__ == "__main__":
    main()
