from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data


EXPERIMENT_ID = "20260911_S003_EX62"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _design(frame: pd.DataFrame, include_gap: bool) -> tuple[np.ndarray, list[str]]:
    continuous = [
        "prior_return_1d", "prior_return_5d", "prior_volatility_20d",
        "prior_trend_60d", "prior_amount_ratio_20d",
    ]
    columns = [*continuous]
    if include_gap:
        columns.append("opening_gap")
    values = frame[columns].copy()
    values = values.sub(values.mean()).div(values.std(ddof=0))
    years = pd.get_dummies(frame.index.year, prefix="year", drop_first=True, dtype=float)
    years.index = frame.index
    values = pd.concat([values, years], axis=1)
    return values.to_numpy(dtype=float), list(values.columns)


def _overlap_result(
    frame: pd.DataFrame,
    outcome_columns: list[str],
    include_gap: bool,
) -> tuple[np.ndarray, pd.DataFrame, float, float]:
    x, names = _design(frame, include_gap)
    treatment = frame["event"].to_numpy(dtype=int)
    model = LogisticRegression(C=1.0, solver="lbfgs", max_iter=1000, random_state=0)
    model.fit(x, treatment)
    propensity = np.clip(model.predict_proba(x)[:, 1], 0.01, 0.99)
    weights = np.where(treatment == 1, 1.0 - propensity, propensity)
    treated = treatment == 1
    control = ~treated

    outcomes = frame[outcome_columns].to_numpy(dtype=float)
    treated_mean = np.average(outcomes[treated], axis=0, weights=weights[treated])
    control_mean = np.average(outcomes[control], axis=0, weights=weights[control])
    effect = treated_mean - control_mean

    balance_rows = []
    for index, name in enumerate(names):
        raw = x[:, index]
        mean_t = np.average(raw[treated], weights=weights[treated])
        mean_c = np.average(raw[control], weights=weights[control])
        variance_t = np.average((raw[treated] - mean_t) ** 2, weights=weights[treated])
        variance_c = np.average((raw[control] - mean_c) ** 2, weights=weights[control])
        pooled = np.sqrt((variance_t + variance_c) / 2.0)
        smd = 0.0 if pooled == 0 else (mean_t - mean_c) / pooled
        balance_rows.append({
            "feature": name,
            "treated_weighted_mean": mean_t,
            "control_weighted_mean": mean_c,
            "standardized_mean_difference": smd,
        })
    ess_t = float(weights[treated].sum() ** 2 / np.square(weights[treated]).sum())
    ess_c = float(weights[control].sum() ** 2 / np.square(weights[control]).sum())
    return effect, pd.DataFrame(balance_rows), ess_t, ess_c


def _bootstrap(
    frame: pd.DataFrame,
    outcome: str,
    include_gap: bool,
    iterations: int,
    seed: int,
) -> np.ndarray:
    periods = pd.PeriodIndex(frame.index, freq="M")
    unique = periods.unique().sort_values()
    groups = [np.flatnonzero(periods == period) for period in unique]
    rng = np.random.default_rng(seed)
    results = []
    attempts = 0
    while len(results) < iterations:
        attempts += 1
        if attempts > iterations * 2:
            raise ValueError("too many invalid bootstrap propensity samples")
        selected = rng.integers(0, len(groups), size=len(groups))
        rows = np.concatenate([groups[item] for item in selected])
        sample = frame.iloc[rows].copy()
        if sample["event"].nunique() != 2:
            continue
        effect, _, _, _ = _overlap_result(sample, [outcome], include_gap)
        results.append(float(effect[0]))
    return np.asarray(results)


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
        raise ValueError("EX62 is diagnostic only")

    dataset = protocol["dataset"]
    ex61 = repo / "experiments/S003/20260911_S003_EX61"
    source = repo / "experiments/S003" / dataset["event_experiment"]
    validate_experiment_archive(ex61)
    validate_experiment_archive(source)
    expected = {
        ex61 / "experiment_manifest.json": dataset["ex61_manifest_sha256"],
        source / "artifacts/mechanism_events.csv": dataset["event_file_sha256"],
        repo / "data/raw/510500_intraday_manifest.json": dataset["intraday_manifest_sha256"],
        repo / "data/raw/510500_manifest.json": dataset["daily_manifest_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path}")

    start = pd.Timestamp(dataset["evaluation_start"])
    cutoff = pd.Timestamp(dataset["development_cutoff"])
    event_dates = pd.DatetimeIndex(pd.to_datetime(
        pd.read_csv(source / "artifacts/mechanism_events.csv")["event_date"]
    ).dt.normalize())
    event_dates = event_dates[(event_dates >= start) & (event_dates <= cutoff)]

    intraday = load_intraday_research_data(repo / "data/raw", "510500.SH")
    bars = intraday.frames["5m"].copy()
    bars["date"] = pd.to_datetime(bars["Date"]).dt.normalize()
    bars["clock"] = pd.to_datetime(bars["Date"]).dt.strftime("%H:%M")
    bars = bars.loc[(bars["date"] >= start) & (bars["date"] <= cutoff)]
    close_path = bars.pivot(index="date", columns="clock", values="Close").sort_index()
    opening = bars.loc[bars["clock"].eq("09:35")].set_index("date")["Open"].astype(float)
    returns = close_path.div(opening, axis=0).sub(1.0)
    outcome_columns = [f"return_{clock.replace(':', '')}" for clock in returns.columns]
    returns.columns = outcome_columns

    daily = load_market_data(repo / "data/raw", "510500.SH").daily.copy()
    daily["dt"] = pd.to_datetime(daily["dt"]).dt.normalize()
    daily = daily.loc[daily["dt"].le(cutoff)].set_index("dt").sort_index()
    daily_return = daily["close"].pct_change()
    frame = pd.DataFrame(index=daily.index)
    frame["prior_return_1d"] = daily_return.shift(1)
    frame["prior_return_5d"] = daily["close"].shift(1).div(daily["close"].shift(6)).sub(1.0)
    frame["prior_volatility_20d"] = daily_return.shift(1).rolling(20).std()
    frame["prior_trend_60d"] = daily["close"].shift(1).div(
        daily["close"].shift(1).rolling(60).mean()
    ).sub(1.0)
    frame["prior_amount_ratio_20d"] = daily["amount"].shift(1).div(
        daily["amount"].shift(1).rolling(20).median()
    )
    frame["opening_gap"] = opening.div(daily["close"].shift(1)).sub(1.0)
    frame["event"] = frame.index.isin(event_dates).astype(int)
    frame = frame.join(returns)
    required = [
        "prior_return_1d", "prior_return_5d", "prior_volatility_20d",
        "prior_trend_60d", "prior_amount_ratio_20d", "opening_gap", *outcome_columns,
    ]
    frame = frame.loc[(frame.index >= start) & (frame.index <= cutoff)].dropna(subset=required)

    model_rows = []
    balance_outputs = []
    path_output = None
    bootstrap = protocol["bootstrap"]
    iterations = int(bootstrap["iterations"])
    seed = int(bootstrap["seed"])
    for ordinal, (name, include_gap) in enumerate((
        ("PRIOR_STATE", False),
        ("PRIOR_STATE_PLUS_OPENING_GAP", True),
    )):
        effect, balance, ess_t, ess_c = _overlap_result(frame, outcome_columns, include_gap)
        draws = _bootstrap(frame, "return_1130", include_gap, iterations, seed + ordinal)
        max_smd = float(balance["standardized_mean_difference"].abs().max())
        model_rows.append({
            "model": name,
            "effect_11_30": float(effect[outcome_columns.index("return_1130")]),
            "lower_90": float(np.quantile(draws, 0.05)),
            "upper_90": float(np.quantile(draws, 0.95)),
            "positive_probability": float((draws > 0).mean()),
            "maximum_absolute_smd": max_smd,
            "treated_effective_sample_size": ess_t,
            "control_effective_sample_size": ess_c,
            "observations": int(len(frame)),
        })
        balance.insert(0, "model", name)
        balance_outputs.append(balance)
        if include_gap:
            path_output = pd.DataFrame({
                "clock": [column.removeprefix("return_")[:2] + ":" + column[-2:] for column in outcome_columns],
                "overlap_weighted_effect": effect,
            })

    models = pd.DataFrame(model_rows)
    balance = pd.concat(balance_outputs, ignore_index=True)
    assert path_output is not None
    raw_event = frame.loc[frame["event"].eq(1), outcome_columns].mean()
    path_output["raw_event_mean_return"] = raw_event.to_numpy()
    raw_peak = path_output.loc[path_output["raw_event_mean_return"].idxmax()]
    raw_post = frame.loc[frame["event"].eq(1), "return_1500"].sub(
        frame.loc[frame["event"].eq(1), "return_1130"]
    )
    gap_model = models.loc[models["model"].eq("PRIOR_STATE_PLUS_OPENING_GAP")].iloc[0]
    balance_limit = float(protocol["interpretation"]["balance_max_absolute_smd"])
    balanced = bool(gap_model["maximum_absolute_smd"] <= balance_limit)
    if not balanced:
        explanation = "WEIGHTING_BALANCE_FAILED"
    elif gap_model["lower_90"] > 0:
        explanation = "OBSERVED_STATE_AND_GAP_DO_NOT_EXPLAIN"
    else:
        explanation = "UNRESOLVED_AFTER_STATE_AND_GAP_CONTROL"
    morning = bool(str(raw_peak["clock"]) <= "11:30" and raw_post.mean() <= 0)
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "event_count": int(frame["event"].sum()),
        "non_event_count": int(frame["event"].eq(0).sum()),
        "absolute_timing_label": "ABSOLUTE_RETURN_MORNING_PEAK" if morning else "ABSOLUTE_RETURN_NOT_MORNING_PEAK",
        "competing_explanation_label": explanation,
        "metrics": {
            "raw_event_peak_clock": str(raw_peak["clock"]),
            "raw_event_peak_return": float(raw_peak["raw_event_mean_return"]),
            "raw_event_return_11_30": float(raw_event["return_1130"]),
            "raw_event_return_15_00": float(raw_event["return_1500"]),
            "raw_post_11_30_increment": float(raw_post.mean()),
            "gap_model_effect_11_30": float(gap_model["effect_11_30"]),
            "gap_model_lower_90": float(gap_model["lower_90"]),
            "gap_model_upper_90": float(gap_model["upper_90"]),
            "gap_model_positive_probability": float(gap_model["positive_probability"]),
            "gap_model_maximum_absolute_smd": float(gap_model["maximum_absolute_smd"]),
            "gap_model_treated_ess": float(gap_model["treated_effective_sample_size"]),
            "gap_model_control_ess": float(gap_model["control_effective_sample_size"]),
        },
        "candidate_changed": False,
        "sm_changed": False,
        "pte_changed": False,
    }
    models.to_csv(artifacts / "overlap_models.csv", index=False, lineterminator="\n")
    balance.to_csv(artifacts / "covariate_balance.csv", index=False, lineterminator="\n")
    path_output.to_csv(artifacts / "overlap_intraday_path.csv", index=False, lineterminator="\n")
    _write_json(artifacts / "robustness_summary.json", summary)

    metrics = summary["metrics"]
    (experiment / "03_execution.md").write_text(
        "# S003 EX62 执行\n\n"
        f"重叠加权复核：`PASS`。事件{summary['event_count']}日、非事件"
        f"{summary['non_event_count']}日；含开盘缺口模型最大绝对SMD为"
        f"{metrics['gap_model_maximum_absolute_smd']:.3f}，事件效应"
        f"{metrics['gap_model_effect_11_30']:.2%}，自然月区块Bootstrap 90%区间"
        f"[{metrics['gap_model_lower_90']:.2%}, {metrics['gap_model_upper_90']:.2%}]。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX62 结论\n\n"
        f"绝对时序标签：`{summary['absolute_timing_label']}`；竞争解释标签："
        f"`{summary['competing_explanation_label']}`。事件原始收益峰值位于"
        f"{metrics['raw_event_peak_clock']}（{metrics['raw_event_peak_return']:.2%}），"
        f"11:30至收盘平均增量{metrics['raw_post_11_30_increment']:.2%}。"
        "结果只用于理解机制，不修改S003-v1。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "strategy_id": "S003",
        "release_id": "S003-v1",
        "symbol": "510500.SH",
        "development_cutoff": str(cutoff.date()),
        "status": "PASS",
        "absolute_timing_label": summary["absolute_timing_label"],
        "competing_explanation_label": summary["competing_explanation_label"],
        "candidate_generation": False,
        "mutates_strategy_manager": False,
        "mutates_pte": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
