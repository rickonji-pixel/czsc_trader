from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data


EXPERIMENT_ID = "20260911_S003_EX66"
FEATURES = [
    "prior_return_1d",
    "prior_return_5d",
    "prior_volatility_20d",
    "prior_trend_60d",
    "prior_amount_ratio_20d",
    "opening_gap",
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _match(frame: pd.DataFrame, caliper: float) -> pd.DataFrame:
    means = frame[FEATURES].mean()
    scales = frame[FEATURES].std(ddof=0)
    standardized = frame[FEATURES].sub(means).div(scales)
    pairs: list[dict[str, object]] = []
    for year in sorted(frame.index.year.unique()):
        treated = frame.loc[(frame.index.year == year) & frame["event"].eq(1)]
        controls = frame.loc[(frame.index.year == year) & frame["event"].eq(0)]
        if treated.empty or controls.empty:
            continue
        treated_z = standardized.loc[treated.index, FEATURES].to_numpy(dtype=float)
        control_z = standardized.loc[controls.index, FEATURES].to_numpy(dtype=float)
        distances = np.sqrt(np.square(treated_z[:, None, :] - control_z[None, :, :]).sum(axis=2))
        prior_distance = np.abs(treated_z[:, None, 0] - control_z[None, :, 0])
        legal = prior_distance <= caliper
        cost = distances + np.where(legal, 0.0, 1_000_000.0)
        rows, columns = linear_sum_assignment(cost)
        for row, column in zip(rows, columns, strict=True):
            if not legal[row, column]:
                continue
            event_date = treated.index[row]
            control_date = controls.index[column]
            pairs.append({
                "event_date": event_date,
                "control_date": control_date,
                "calendar_year": int(year),
                "prior_return_distance_sd": float(prior_distance[row, column]),
                "standardized_distance": float(distances[row, column]),
                "event_return_1130": float(treated.iloc[row]["return_1130"]),
                "control_return_1130": float(controls.iloc[column]["return_1130"]),
            })
    result = pd.DataFrame(pairs)
    if result.empty:
        raise ValueError("no legal matched pairs")
    result["paired_effect"] = result["event_return_1130"] - result["control_return_1130"]
    return result


def _balance(frame: pd.DataFrame, pairs: pd.DataFrame) -> pd.DataFrame:
    event_dates = pd.DatetimeIndex(pairs["event_date"])
    control_dates = pd.DatetimeIndex(pairs["control_date"])
    rows = []
    for name in FEATURES:
        event = frame.loc[event_dates, name].to_numpy(dtype=float)
        control = frame.loc[control_dates, name].to_numpy(dtype=float)
        pooled = float(np.sqrt((event.var() + control.var()) / 2.0))
        rows.append({
            "feature": name,
            "event_mean": float(event.mean()),
            "control_mean": float(control.mean()),
            "standardized_mean_difference": 0.0 if pooled == 0 else float((event.mean() - control.mean()) / pooled),
        })
    return pd.DataFrame(rows)


def _bootstrap(pairs: pd.DataFrame, iterations: int, seed: int) -> np.ndarray:
    periods = pd.PeriodIndex(pd.to_datetime(pairs["event_date"]), freq="M")
    unique = periods.unique().sort_values()
    groups = [np.flatnonzero(periods == period) for period in unique]
    rng = np.random.default_rng(seed)
    effects = pairs["paired_effect"].to_numpy(dtype=float)
    draws = []
    for _ in range(iterations):
        selected = rng.integers(0, len(groups), size=len(groups))
        rows = np.concatenate([groups[item] for item in selected])
        draws.append(float(effects[rows].mean()))
    return np.asarray(draws, dtype=float)


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(protocol.get(key) for key in (
        "parameter_selection", "candidate_generation", "promotion_allowed",
        "mutates_strategy_manager", "mutates_pte",
    )):
        raise ValueError("EX66 is diagnostic only")

    dataset = protocol["dataset"]
    ex65 = repo / "experiments/S003/20260911_S003_EX65"
    source = repo / "experiments/S003" / dataset["event_experiment"]
    validate_experiment_archive(ex65)
    validate_experiment_archive(source)
    expected = {
        ex65 / "experiment_manifest.json": dataset["ex65_manifest_sha256"],
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
    opening = bars.loc[bars["clock"].eq("09:35")].set_index("date")["Open"].astype(float)
    close_1130 = bars.loc[bars["clock"].eq("11:30")].set_index("date")["Close"].astype(float)

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
    frame["return_1130"] = close_1130.div(opening).sub(1.0)
    frame["event"] = frame.index.isin(event_dates).astype(int)
    frame = frame.loc[(frame.index >= start) & (frame.index <= cutoff)].dropna(
        subset=[*FEATURES, "return_1130"]
    )

    method = protocol["method"]
    pairs = _match(frame, float(method["prior_return_caliper_standard_deviation"]))
    balance = _balance(frame, pairs)
    bootstrap = protocol["bootstrap"]
    draws = _bootstrap(pairs, int(bootstrap["iterations"]), int(bootstrap["seed"]))
    lower = float(np.quantile(draws, 0.05))
    upper = float(np.quantile(draws, 0.95))
    limits = protocol["interpretation"]
    event_total = int(frame["event"].sum())
    retention = len(pairs) / event_total
    prior_smd = float(balance.loc[
        balance["feature"].eq("prior_return_1d"), "standardized_mean_difference"
    ].abs().iloc[0])
    max_smd = float(balance["standardized_mean_difference"].abs().max())
    max_pair_distance = float(pairs["prior_return_distance_sd"].max())
    gates = {
        "event_retention": retention >= float(limits["minimum_event_retention"]),
        "pair_prior_return_distance": max_pair_distance <= float(
            limits["maximum_pair_prior_return_distance_sd"]
        ),
        "prior_return_smd": prior_smd <= float(limits["prior_return_max_absolute_smd"]),
        "continuous_smd": max_smd <= float(limits["continuous_max_absolute_smd"]),
    }
    effect = float(pairs["paired_effect"].mean())
    if not all(gates.values()):
        label = "MATCHING_QUALITY_FAILED"
    elif lower > 0:
        label = "MONEYFLOW_INCREMENT_SUPPORTED_IN_COMMON_SUPPORT"
    elif upper <= 0:
        label = "MOMENTUM_EXPLANATION_SUPPORTED_IN_COMMON_SUPPORT"
    else:
        label = "INCREMENT_UNRESOLVED_IN_COMMON_SUPPORT"

    pairs_output = pairs.copy()
    pairs_output["event_date"] = pd.to_datetime(pairs_output["event_date"]).dt.strftime("%Y-%m-%d")
    pairs_output["control_date"] = pd.to_datetime(pairs_output["control_date"]).dt.strftime("%Y-%m-%d")
    pairs_output.to_csv(artifacts / "matched_pairs.csv", index=False, lineterminator="\n")
    balance.to_csv(artifacts / "covariate_balance.csv", index=False, lineterminator="\n")
    pd.DataFrame({"paired_effect_11_30": draws}).to_csv(
        artifacts / "bootstrap_effects.csv", index=False, lineterminator="\n"
    )
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "event_count": event_total,
        "matched_pair_count": int(len(pairs)),
        "competing_explanation_label": label,
        "gates": gates,
        "metrics": {
            "event_retention": float(retention),
            "paired_effect": effect,
            "effect_lower_90": lower,
            "effect_upper_90": upper,
            "effect_positive_probability": float((draws > 0).mean()),
            "event_mean_return_11_30": float(pairs["event_return_1130"].mean()),
            "control_mean_return_11_30": float(pairs["control_return_1130"].mean()),
            "maximum_pair_prior_return_distance_sd": max_pair_distance,
            "prior_return_absolute_smd": prior_smd,
            "continuous_maximum_absolute_smd": max_smd,
        },
        "scope": "COMMON_SUPPORT_SUBSET_ONLY",
        "candidate_changed": False,
        "sm_changed": False,
        "pte_changed": False,
    }
    _write_json(artifacts / "robustness_summary.json", summary)
    metrics = summary["metrics"]
    (experiment / "03_execution.md").write_text(
        "# S003 EX66 执行\n\n"
        f"共同支持配对复核：`PASS`。276个事件中匹配{len(pairs)}个，保留率"
        f"{retention:.1%}；前一日收益SMD为{prior_smd:.4f}，全部连续变量最大SMD为"
        f"{max_smd:.4f}。配对事件效应{effect:.2%}，自然月区块Bootstrap 90%区间"
        f"[{lower:.2%}, {upper:.2%}]。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX66 结论\n\n"
        f"竞争解释标签：`{label}`。结论只适用于能够找到严格可比非事件日的共同支持子样本，"
        "不得外推至被匹配排除的极端事件，也不修改S003-v1。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "strategy_id": "S003",
        "release_id": "S003-v1",
        "symbol": "510500.SH",
        "development_cutoff": str(cutoff.date()),
        "status": "PASS",
        "competing_explanation_label": label,
        "candidate_generation": False,
        "mutates_strategy_manager": False,
        "mutates_pte": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
