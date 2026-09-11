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
from czsc_trader.intraday_opening_execution import _performance, _trade_return
from czsc_trader.intraday_opportunity_map import _price_checkpoints
from czsc_trader.relative_style_evaluation import evaluate_relative_style_events


EXPERIMENT_ID = "20260911_S003_EX46"
SELECTED_MECHANISM = "CONSTITUENT_MONEYFLOW_BREADTH_CONTINUATION"


def _read(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _group_keys(frame: pd.DataFrame) -> list[str]:
    if "hypothesis_id" in frame:
        return ["hypothesis_id", *[key for key in ("exit_clock", "exit_bars") if key in frame]]
    if "mechanism_id" in frame:
        return [
            "mechanism_id",
            *[key for key in ("variant", "direction", "exit_clock") if key in frame],
        ]
    if "segment_id" in frame:
        return ["segment_id", "entry_checkpoint", "exit_checkpoint"]
    return [key for key in ("mechanism", "variant", "direction") if key in frame]


def _date_key(frame: pd.DataFrame) -> str:
    for key in ("trade_date", "event_date", "exit_date"):
        if key in frame:
            return key
    raise ValueError("trial episode file has no usable date")


def _trial_matrix(
    repo: Path,
    inventory: list[dict[str, str]],
    calendar: pd.DatetimeIndex,
    fraction: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    columns: dict[str, pd.Series] = {}
    ledger: list[dict[str, object]] = []
    for source in inventory:
        path = repo / source["file"]
        experiment = path.parents[1]
        validate_experiment_archive(experiment)
        if _sha256(path) != source["sha256"]:
            raise ValueError(f"trial source differs: {path}")
        if _sha256(experiment / "experiment_manifest.json") != source["manifest_sha256"]:
            raise ValueError(f"trial manifest differs: {experiment.name}")
        frame = pd.read_csv(path)
        date_key = _date_key(frame)
        frame[date_key] = pd.to_datetime(frame[date_key]).dt.normalize()
        keys = _group_keys(frame)
        for values, rows in frame.groupby(keys, sort=True, observed=True, dropna=False):
            values = values if isinstance(values, tuple) else (values,)
            identity = ":".join(str(value) for value in values)
            trial_id = f"{source['experiment_id']}:{identity}"
            daily = pd.Series(0.0, index=calendar)
            in_scope = rows.loc[rows[date_key].isin(calendar)].copy()
            grouped = in_scope.groupby(date_key, sort=True)["stress_return"].sum().astype(float)
            daily.loc[grouped.index] = grouped.to_numpy() * fraction
            dispersion = float(daily.std(ddof=1))
            included = bool(np.isfinite(dispersion) and dispersion > 0)
            ledger.append(
                {
                    "trial_id": trial_id,
                    "experiment_id": source["experiment_id"],
                    "group_keys": ",".join(keys),
                    "episode_rows": int(len(in_scope)),
                    "active_days": int(daily.ne(0).sum()),
                    "included": included,
                    "exclusion_reason": "" if included else "ZERO_OR_INVALID_DISPERSION",
                }
            )
            if included:
                columns[trial_id] = daily
    return pd.DataFrame(columns, index=calendar).sort_index(axis=1), pd.DataFrame(ledger)


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


def _neighbor_audit(
    features: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    intraday: pd.DataFrame,
    quantiles: list[float],
    *,
    lookback: int,
    stress_cost: float,
    base_fraction: float,
    start: str,
) -> pd.DataFrame:
    feature = features.copy()
    feature["dt"] = pd.to_datetime(feature["dt"]).dt.normalize()
    feature = feature.set_index("dt").sort_index()
    next_session = pd.Series(calendar[1:], index=calendar[:-1])
    rows: list[dict[str, object]] = []
    for quantile in quantiles:
        threshold = (
            feature["moneyflow_breadth"]
            .shift(1)
            .rolling(lookback, min_periods=lookback)
            .quantile(quantile)
        )
        selected = feature.loc[feature["moneyflow_breadth"].ge(threshold)].copy()
        selected["event_date"] = selected.index.map(next_session)
        selected = selected.loc[selected["event_date"].notna()].reset_index(names="signal_date")
        selected.insert(0, "mechanism", SELECTED_MECHANISM)
        result = evaluate_relative_style_events(
            selected,
            intraday,
            {SELECTED_MECHANISM: {"entry_checkpoint": "OPEN", "exit_checkpoint": "11:30_CLOSE"}},
            evaluation_start=start,
            baseline_one_way_cost=0.00012,
            stress_one_way_cost=stress_cost,
            base_fraction=base_fraction,
            positive_years_required=4,
            recent_sessions=252,
        )
        metric = result.metrics.iloc[0]
        rows.append(
            {
                "quantile": quantile,
                "events": int(metric["episodes"]),
                "stress_mean_return": float(metric["stress_mean_return"]),
                "stress_profit_factor": float(metric["stress_profit_factor"]),
                "positive_years": int(metric["positive_years"]),
                "recent_stress_mean_return": float(metric["recent_stress_mean_return"]),
                "incremental_terminal_return_vs_static": float(
                    metric["incremental_terminal_return_vs_static"]
                ),
            }
        )
    return pd.DataFrame(rows)


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
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX46 may only audit frozen evidence")

    dataset = protocol["dataset"]
    implementation = protocol["se_implementation"]
    expected_implementation = {
        repo / "packages/strategy_evaluator/src/strategy_evaluator/bootstrap.py": implementation[
            "bootstrap_sha256"
        ],
        repo / "packages/strategy_evaluator/src/strategy_evaluator/search_bias.py": implementation[
            "search_bias_sha256"
        ],
        repo / dataset["feature_file"]: dataset["feature_file_sha256"],
        repo / "data/raw/510500_intraday_manifest.json": dataset["intraday_manifest_sha256"],
    }
    for path, digest in expected_implementation.items():
        if _sha256(path) != digest:
            raise ValueError(f"audit source differs: {path}")

    inventory = _read(artifacts / protocol["trial_universe"]["inventory_file"].split("/", 1)[1])
    intraday = load_intraday_research_data(repo / "data/raw", "510500.SH")
    checkpoints = _price_checkpoints(intraday.frames["5m"])
    start = pd.Timestamp(dataset["evaluation_start"])
    calendar = checkpoints.loc[
        (checkpoints.index >= start) & (checkpoints.index <= pd.Timestamp(dataset["development_cutoff"]))
    ].index
    fraction = float(protocol["trial_universe"]["daily_capital_fraction"])
    matrix, ledger = _trial_matrix(repo, inventory, calendar, fraction)
    raw_count = int(protocol["trial_universe"]["expected_raw_trial_count"])
    if len(ledger) != raw_count or len(matrix.columns) != raw_count:
        raise ValueError(
            f"trial count differs from protocol: ledger={len(ledger)} matrix={len(matrix.columns)}"
        )
    selected_id = (
        "20260911_S003_EX45:CONSTITUENT_MONEYFLOW_BREADTH_CONTINUATION:PRIMARY_LONG:1"
    )
    if selected_id not in matrix:
        raise ValueError("selected trial missing from search universe")

    evidence = ReturnMatrixEvidence(
        tuple(date.strftime("%Y-%m-%d") for date in calendar),
        tuple(map(str, matrix.columns)),
        tuple(tuple(float(value) for value in row) for row in matrix.to_numpy()),
        "",
    )
    evidence = ReturnMatrixEvidence(
        evidence.dates, evidence.candidate_ids, evidence.returns, hash_return_matrix(evidence)
    )
    audit = protocol["audit"]
    pbo = cscv_pbo(evidence, int(audit["cscv_blocks"]))
    sharpes = np.asarray([annualized_sharpe(matrix[column].to_numpy()) for column in matrix])
    effective_count = effective_trial_count(matrix.to_numpy())
    selected_daily = matrix[selected_id]
    dsr = calculate_dsr_bundle(
        selected_daily.to_numpy(),
        sharpes,
        raw_count=raw_count,
        effective_count=effective_count,
    )

    bootstrap_reports = []
    bootstrap_rows: list[dict[str, object]] = []
    for block in audit["bootstrap_block_lengths"]:
        report = stationary_bootstrap_performance(
            selected_daily.to_numpy(),
            candidate_id=selected_id,
            repetitions=int(audit["bootstrap_repetitions"]),
            mean_block_length=int(block),
            seed=int(audit["seed"]) + int(block),
        )
        bootstrap_reports.append(report)
        for metric in (report.cagr, report.max_drawdown, report.calmar):
            bootstrap_rows.append({"mean_block_length": int(block), **asdict(metric)})

    all_morning_stress = pd.Series(
        [
            _trade_return(
                float(checkpoints.loc[date, "OPEN"]),
                float(checkpoints.loc[date, "11:30_CLOSE"]),
                1,
                float(protocol["execution"]["stress_one_way_cost"]),
            )
            * fraction
            for date in calendar
        ],
        index=calendar,
    )
    active = selected_daily.ne(0).astype(int)
    observed_placebo_mean = float(all_morning_stress.loc[active.eq(1)].mean())
    flags = active.to_numpy()
    morning = all_morning_stress.to_numpy()
    placebo_means = np.asarray(
        [float(morning[np.roll(flags, lag).astype(bool)].mean()) for lag in range(1, len(flags))]
    )
    placebo_percentile = float(np.mean(placebo_means <= observed_placebo_mean))

    current_source = repo / "experiments/20260911_S003_EX45/artifacts/episodes.csv.gz"
    current = pd.read_csv(current_source)
    current = current.loc[current["variant"].eq("PRIMARY_LONG")].copy()
    current["event_date"] = pd.to_datetime(current["event_date"])
    loyo_rows: list[dict[str, object]] = []
    for removed_year in sorted(current["event_date"].dt.year.unique()):
        kept = current.loc[current["event_date"].dt.year.ne(removed_year)]
        result = _performance(
            kept["stress_return"],
            max((kept["event_date"].max() - kept["event_date"].min()).days / 365.25, 1.0),
        )
        loyo_rows.append(
            {
                "removed_year": int(removed_year),
                "episodes": int(len(kept)),
                "stress_mean_return": result["mean_return"],
                "stress_profit_factor": result["profit_factor"],
                "stress_cumulative_return": result["cumulative_return"],
            }
        )
    loyo = pd.DataFrame(loyo_rows)
    recent = selected_daily.iloc[-int(audit["recent_sessions"]):]
    recent_events = int(recent.ne(0).sum())
    recent_mean = float(recent.loc[recent.ne(0)].mean()) if recent_events else 0.0
    features = pd.read_csv(repo / dataset["feature_file"])
    neighbors = _neighbor_audit(
        features,
        calendar,
        intraday.frames["5m"],
        [float(value) for value in audit["neighbor_quantiles"]],
        lookback=int(audit["threshold_lookback_sessions"]),
        stress_cost=float(protocol["execution"]["stress_one_way_cost"]),
        base_fraction=fraction,
        start=dataset["evaluation_start"],
    )

    direction_flags = {
        "pbo": _label(
            pbo.pbo,
            float(audit["favorable_pbo_max"]),
            float(audit["adverse_pbo_min_exclusive"]),
            lower_better=True,
        ),
        "dsr": _label(
            dsr.effective.probability,
            float(audit["positive_dsr_probability"]),
            float(audit["adverse_dsr_probability_below"]),
            lower_better=False,
        ),
        "absolute_bootstrap": (
            "FAVORABLE"
            if all(report.cagr.lower_90 > 0 and report.calmar.lower_90 > 0 for report in bootstrap_reports)
            else "ADVERSE"
            if all(report.cagr.probability_above_zero < 0.5 for report in bootstrap_reports)
            else "MIXED"
        ),
        "cyclic_placebo": _label(
            placebo_percentile,
            float(audit["favorable_placebo_percentile"]),
            float(audit["adverse_placebo_percentile_below"]),
            lower_better=False,
        ),
        "leave_one_year_out": (
            "FAVORABLE"
            if loyo["stress_mean_return"].gt(0).all()
            and loyo["stress_profit_factor"].gt(1).all()
            else "MIXED"
        ),
        "threshold_neighborhood": (
            "FAVORABLE"
            if neighbors["stress_mean_return"].gt(0).all()
            and neighbors["stress_profit_factor"].gt(1).all()
            else "ADVERSE"
            if float(neighbors.loc[neighbors["quantile"].eq(0.8), "stress_mean_return"].iloc[0]) <= 0
            else "MIXED"
        ),
        "recent_252_sessions": "FAVORABLE" if recent_mean > 0 else "ADVERSE",
    }
    if "ADVERSE" in direction_flags.values():
        overall = "ADVERSE"
    elif set(direction_flags.values()) == {"FAVORABLE"}:
        overall = "FAVORABLE"
    else:
        overall = "MIXED"

    ledger.to_csv(artifacts / "trial_ledger.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    pd.DataFrame({"trial_id": matrix.columns, "annualized_sharpe": sharpes}).to_csv(
        artifacts / "trial_sharpes.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    pd.DataFrame([asdict(item) for item in pbo.splits]).to_csv(
        artifacts / "cscv_splits.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    pd.DataFrame(bootstrap_rows).to_csv(
        artifacts / "bootstrap_intervals.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    loyo.to_csv(artifacts / "leave_one_year_out.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
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
        "direction_flags": direction_flags,
        "raw_trial_count": raw_count,
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
            "return_matrix_hash": evidence.content_hash,
            "pbo": pbo.to_dict(),
            "dsr": {"raw": asdict(dsr.raw), "effective": asdict(dsr.effective)},
            "bootstrap": [asdict(report) for report in bootstrap_reports],
        },
    )
    flag_table = ["|审计方向|标签|", "|---|---|"] + [
        f"|{name}|`{label}`|" for name, label in direction_flags.items()
    ]
    (experiment / "03_execution.md").write_text(
        "# S003 EX46 执行\n\n"
        f"状态：COMPLETE。完成{raw_count}条历史试验路径的搜索偏差审计、3档区块Bootstrap、"
        f"{len(placebo_means)}次循环位移、逐年删除、最近252日和3档阈值邻域审计。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX46 结论\n\n"
        f"总标签：`{overall}`。PBO为{pbo.pbo:.2%}，有效试验数{effective_count:.2f}，"
        f"有效DSR概率{dsr.effective.probability:.2%}。循环位移分位{placebo_percentile:.2%}；"
        f"最近252交易日包含{recent_events}个事件，半仓压力收益均值{recent_mean:.3%}。\n\n"
        + "\n".join(flag_table)
        + "\n\n"
        + (
            "存在ADVERSE证据，停止该原型，不创建候选。"
            if overall == "ADVERSE"
            else "没有触发停止条件，可进入研究候选登记评审。"
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
            "development_cutoff": dataset["development_cutoff"],
            "evidence_label": overall,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
