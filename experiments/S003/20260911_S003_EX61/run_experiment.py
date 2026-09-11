from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data


EXPERIMENT_ID = "20260911_S003_EX61"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _block_means(values: pd.DataFrame, iterations: int, seed: int) -> np.ndarray:
    months = pd.PeriodIndex(values.index, freq="M")
    unique = months.unique().sort_values()
    groups = [np.flatnonzero(months == month) for month in unique]
    rng = np.random.default_rng(seed)
    draws = np.empty((iterations, values.shape[1]), dtype=float)
    raw = values.to_numpy(dtype=float)
    for index in range(iterations):
        selected = rng.integers(0, len(groups), size=len(groups))
        rows = np.concatenate([groups[item] for item in selected])
        draws[index] = np.nanmean(raw[rows], axis=0)
    return draws


def _regression_event_coefficient(frame: pd.DataFrame, features: list[str]) -> float:
    columns = ["event", *features]
    clean = frame[["return_1130", *columns]].dropna()
    design = np.column_stack(
        [np.ones(len(clean)), clean[columns].to_numpy(dtype=float)]
    )
    coefficient = np.linalg.lstsq(
        design, clean["return_1130"].to_numpy(dtype=float), rcond=None
    )[0]
    return float(coefficient[1])


def _bootstrap_regression(
    frame: pd.DataFrame,
    features: list[str],
    iterations: int,
    seed: int,
) -> np.ndarray:
    clean = frame[["return_1130", "event", *features]].dropna().copy()
    months = pd.PeriodIndex(clean.index, freq="M")
    unique = months.unique().sort_values()
    groups = [np.flatnonzero(months == month) for month in unique]
    rng = np.random.default_rng(seed)
    draws = np.empty(iterations, dtype=float)
    for index in range(iterations):
        selected = rng.integers(0, len(groups), size=len(groups))
        rows = np.concatenate([groups[item] for item in selected])
        draws[index] = _regression_event_coefficient(clean.iloc[rows], features)
    return draws


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        protocol.get(key)
        for key in (
            "parameter_selection", "candidate_generation", "promotion_allowed",
            "mutates_strategy_manager", "mutates_pte",
        )
    ):
        raise ValueError("EX61 is diagnostic only")

    dataset = protocol["dataset"]
    source = repo / "experiments" / "S003" / dataset["event_experiment"]
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": dataset["event_manifest_sha256"],
        source / "artifacts" / "mechanism_events.csv": dataset["event_file_sha256"],
        repo / "data/raw/510500_intraday_manifest.json": dataset["intraday_manifest_sha256"],
        repo / "data/raw/510500_manifest.json": dataset["daily_manifest_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path}")

    cutoff = pd.Timestamp(dataset["development_cutoff"])
    start = pd.Timestamp(dataset["evaluation_start"])
    events = pd.read_csv(source / "artifacts/mechanism_events.csv")
    event_dates = pd.DatetimeIndex(pd.to_datetime(events["event_date"]).dt.normalize())
    event_dates = event_dates[(event_dates >= start) & (event_dates <= cutoff)]

    intraday = load_intraday_research_data(repo / "data/raw", "510500.SH")
    bars = intraday.frames["5m"].copy()
    bars["date"] = pd.to_datetime(bars["Date"]).dt.normalize()
    bars["clock"] = pd.to_datetime(bars["Date"]).dt.strftime("%H:%M")
    bars = bars.loc[(bars["date"] >= start) & (bars["date"] <= cutoff)]
    close_path = bars.pivot(index="date", columns="clock", values="Close").sort_index()
    amount_path = bars.pivot(index="date", columns="clock", values="Amount").sort_index()
    clocks = list(close_path.columns)
    required_clocks = {"09:35", "11:30", "15:00"}
    if not required_clocks.issubset(clocks):
        raise ValueError("5-minute data lacks a required checkpoint")
    opening = bars.loc[bars["clock"].eq("09:35")].set_index("date")["Open"].astype(float)
    returns = close_path.div(opening, axis=0).sub(1.0)
    cumulative_amount_share = amount_path.cumsum(axis=1).div(amount_path.sum(axis=1), axis=0)

    daily = load_market_data(repo / "data/raw", "510500.SH").daily.copy()
    daily["dt"] = pd.to_datetime(daily["dt"]).dt.normalize()
    daily = daily.loc[daily["dt"].le(cutoff)].set_index("dt").sort_index()
    daily_return = daily["close"].pct_change()
    known = pd.DataFrame(index=daily.index)
    known["prior_return_1d"] = daily_return.shift(1)
    known["prior_return_5d"] = daily["close"].shift(1).div(daily["close"].shift(6)).sub(1.0)
    known["prior_volatility_20d"] = daily_return.shift(1).rolling(20).std()
    known["prior_trend_60d"] = daily["close"].shift(1).div(
        daily["close"].shift(1).rolling(60).mean()
    ).sub(1.0)
    known["prior_amount_ratio_20d"] = daily["amount"].shift(1).div(
        daily["amount"].shift(1).rolling(20).median()
    )
    known["opening_gap"] = opening.div(daily["close"].shift(1)).sub(1.0)
    known["return_1130"] = returns["11:30"]
    known["event"] = known.index.isin(event_dates).astype(int)
    feature_names = list(protocol["matching"]["features"])
    eligible = known[[*feature_names, "opening_gap", "return_1130", "event"]].dropna()
    eligible = eligible.loc[eligible.index.intersection(returns.dropna().index)]

    median = eligible[feature_names].median()
    scale = eligible[feature_names].quantile(0.75) - eligible[feature_names].quantile(0.25)
    if scale.le(0).any():
        raise ValueError("matching feature has zero robust scale")
    standardized = eligible[feature_names].sub(median).div(scale)
    positions = pd.Series(np.arange(len(eligible)), index=eligible.index)
    event_mask = eligible["event"].eq(1)
    proximity = event_mask.astype(int).rolling(5, center=True, min_periods=1).max().astype(bool)
    control_dates = eligible.index[~event_mask & ~proximity]
    k = int(protocol["matching"]["controls_per_event"])
    radius = int(protocol["matching"]["maximum_calendar_distance_sessions"])
    match_rows: list[dict[str, object]] = []
    for event_date in eligible.index[event_mask]:
        distance_in_time = (positions.loc[control_dates] - positions.loc[event_date]).abs()
        local = control_dates[distance_in_time.le(radius)]
        if len(local) < k:
            local = control_dates
        distances = standardized.loc[local].sub(standardized.loc[event_date]).pow(2).sum(axis=1)
        for rank, control_date in enumerate(distances.nsmallest(k).index, start=1):
            match_rows.append({
                "event_date": event_date,
                "control_date": control_date,
                "rank": rank,
                "distance": float(distances.loc[control_date]),
            })
    matches = pd.DataFrame(match_rows)
    if matches["event_date"].nunique() != int(event_mask.sum()):
        raise ValueError("not every event received matched controls")

    event_path = returns.loc[pd.DatetimeIndex(matches["event_date"].unique()), clocks]
    event_path.index.name = "event_date"
    control_path_rows = []
    control_amount_rows = []
    for event_date, group in matches.groupby("event_date", sort=True):
        dates = pd.DatetimeIndex(group["control_date"])
        control_path_rows.append(returns.loc[dates, clocks].mean().rename(event_date))
        control_amount_rows.append(
            cumulative_amount_share.loc[dates, clocks].mean().rename(event_date)
        )
    control_path = pd.DataFrame(control_path_rows)
    control_path.index = pd.DatetimeIndex(control_path.index, name="event_date")
    control_amount = pd.DataFrame(control_amount_rows)
    control_amount.index = pd.DatetimeIndex(control_amount.index, name="event_date")
    event_amount = cumulative_amount_share.loc[event_path.index, clocks]
    paired = event_path - control_path
    bootstrap = protocol["bootstrap"]
    draws = _block_means(
        paired, int(bootstrap["iterations"]), int(bootstrap["seed"])
    )
    path_summary = pd.DataFrame({
        "clock": clocks,
        "event_mean_return": event_path.mean().to_numpy(),
        "control_mean_return": control_path.mean().to_numpy(),
        "abnormal_mean_return": paired.mean().to_numpy(),
        "abnormal_lower_90": np.quantile(draws, 0.05, axis=0),
        "abnormal_upper_90": np.quantile(draws, 0.95, axis=0),
        "abnormal_positive_probability": (draws > 0).mean(axis=0),
        "event_cumulative_amount_share": event_amount.mean().to_numpy(),
        "control_cumulative_amount_share": control_amount.mean().to_numpy(),
    })

    normalized = eligible.copy()
    normalized[feature_names] = standardized
    iterations = int(bootstrap["iterations"])
    seed = int(bootstrap["seed"])
    model_rows = []
    for model, features in (
        ("PRIOR_STATE", feature_names),
        ("PRIOR_STATE_PLUS_OPENING_GAP", [*feature_names, "opening_gap"]),
    ):
        coefficient = _regression_event_coefficient(normalized, features)
        coefficient_draws = _bootstrap_regression(normalized, features, iterations, seed + len(model))
        model_rows.append({
            "model": model,
            "event_coefficient": coefficient,
            "lower_90": float(np.quantile(coefficient_draws, 0.05)),
            "upper_90": float(np.quantile(coefficient_draws, 0.95)),
            "positive_probability": float((coefficient_draws > 0).mean()),
            "observations": int(len(normalized)),
        })
    models = pd.DataFrame(model_rows)

    row_1130 = path_summary.loc[path_summary["clock"].eq("11:30")].iloc[0]
    row_1500 = path_summary.loc[path_summary["clock"].eq("15:00")].iloc[0]
    peak = path_summary.loc[path_summary["abnormal_mean_return"].idxmax()]
    event_post = close_path.loc[event_path.index, "15:00"].div(
        close_path.loc[event_path.index, "11:30"]
    ).sub(1.0)
    control_post = pd.Series({
        event_date: close_path.loc[pd.DatetimeIndex(group["control_date"]), "15:00"].div(
            close_path.loc[pd.DatetimeIndex(group["control_date"]), "11:30"]
        ).sub(1.0).mean()
        for event_date, group in matches.groupby("event_date", sort=True)
    })
    control_post.index = pd.DatetimeIndex(control_post.index)
    post_abnormal = event_post - control_post
    post_draws = _block_means(post_abnormal.to_frame("post"), iterations, seed + 99)[:, 0]
    gap_model = models.loc[models["model"].eq("PRIOR_STATE_PLUS_OPENING_GAP")].iloc[0]
    incremental = bool(row_1130["abnormal_lower_90"] > 0 and gap_model["lower_90"] > 0)
    morning = bool(
        row_1130["abnormal_mean_return"] > 0
        and row_1500["abnormal_mean_return"] <= row_1130["abnormal_mean_return"]
        and str(peak["clock"]) <= "11:30"
    )
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "event_count": int(len(event_path)),
        "matched_control_rows": int(len(matches)),
        "timing_label": "MORNING_CONCENTRATED" if morning else "NOT_CONFINED_TO_MORNING",
        "competing_explanation_label": (
            "OBSERVED_STATE_AND_GAP_DO_NOT_EXPLAIN"
            if incremental else "STATE_OR_GAP_MAY_EXPLAIN"
        ),
        "metrics": {
            "event_return_11_30": float(row_1130["event_mean_return"]),
            "matched_control_return_11_30": float(row_1130["control_mean_return"]),
            "abnormal_return_11_30": float(row_1130["abnormal_mean_return"]),
            "abnormal_return_11_30_lower_90": float(row_1130["abnormal_lower_90"]),
            "abnormal_return_15_00": float(row_1500["abnormal_mean_return"]),
            "peak_abnormal_clock": str(peak["clock"]),
            "peak_abnormal_return": float(peak["abnormal_mean_return"]),
            "post_11_30_abnormal_return": float(post_abnormal.mean()),
            "post_11_30_abnormal_lower_90": float(np.quantile(post_draws, 0.05)),
            "event_amount_share_11_30": float(row_1130["event_cumulative_amount_share"]),
            "control_amount_share_11_30": float(row_1130["control_cumulative_amount_share"]),
            "gap_controlled_event_coefficient": float(gap_model["event_coefficient"]),
            "gap_controlled_event_coefficient_lower_90": float(gap_model["lower_90"]),
        },
        "candidate_changed": False,
        "sm_changed": False,
        "pte_changed": False,
    }

    matches.assign(
        event_date=matches["event_date"].dt.strftime("%Y-%m-%d"),
        control_date=matches["control_date"].dt.strftime("%Y-%m-%d"),
    ).to_csv(artifacts / "matched_controls.csv.gz", index=False,
             compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
             lineterminator="\n")
    path_summary.to_csv(artifacts / "intraday_path.csv", index=False, lineterminator="\n")
    models.to_csv(artifacts / "competition_models.csv", index=False, lineterminator="\n")
    _write_json(artifacts / "mechanism_summary.json", summary)

    metrics = summary["metrics"]
    (experiment / "03_execution.md").write_text(
        "# S003 EX61 执行\n\n"
        f"机制诊断：`PASS`。{summary['event_count']}个事件匹配"
        f"{summary['matched_control_rows']}个控制样本。事件开盘至11:30均值"
        f"{metrics['event_return_11_30']:.2%}，匹配控制{metrics['matched_control_return_11_30']:.2%}，"
        f"异常收益{metrics['abnormal_return_11_30']:.2%}（区块Bootstrap 90%下界"
        f"{metrics['abnormal_return_11_30_lower_90']:.2%}）。加入开盘缺口后事件系数"
        f"{metrics['gap_controlled_event_coefficient']:.2%}，90%下界"
        f"{metrics['gap_controlled_event_coefficient_lower_90']:.2%}。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX61 结论\n\n"
        f"时序标签：`{summary['timing_label']}`；竞争解释标签："
        f"`{summary['competing_explanation_label']}`。异常收益峰值位于"
        f"{metrics['peak_abnormal_clock']}（{metrics['peak_abnormal_return']:.2%}），"
        f"11:30至收盘异常增量{metrics['post_11_30_abnormal_return']:.2%}。"
        "该结果只用于解释冻结机制，不修改S003-v1，不产生新候选。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "strategy_id": "S003",
        "release_id": "S003-v1",
        "symbol": "510500.SH",
        "development_cutoff": str(cutoff.date()),
        "status": "PASS",
        "timing_label": summary["timing_label"],
        "competing_explanation_label": summary["competing_explanation_label"],
        "candidate_generation": False,
        "mutates_strategy_manager": False,
        "mutates_pte": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
