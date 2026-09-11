from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data


EXPERIMENT_ID = "20260911_S003_EX65"
CONTINUOUS = [
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


def _design(frame: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    standardized = frame[CONTINUOUS].copy()
    standardized = standardized.sub(standardized.mean()).div(standardized.std(ddof=0))
    prior = standardized["prior_return_1d"]
    values = pd.DataFrame(index=frame.index)
    values["prior_return_1d_z"] = prior
    values["prior_return_1d_z2"] = prior.square()
    values["prior_return_1d_z3"] = prior.pow(3)
    for name in CONTINUOUS[1:]:
        values[f"{name}_z"] = standardized[name]

    quantiles = pd.qcut(frame["prior_return_1d"], q=10, labels=False, duplicates="drop")
    bins = pd.get_dummies(quantiles, prefix="prior_return_bin", drop_first=True, dtype=float)
    bins.index = frame.index
    years = pd.get_dummies(frame.index.year, prefix="year", drop_first=True, dtype=float)
    years.index = frame.index
    values = pd.concat([values, bins, years], axis=1)
    return values.to_numpy(dtype=float), list(values.columns)


def _dual_objective(x: np.ndarray, target: np.ndarray, coefficient: np.ndarray) -> float:
    logits = x @ coefficient
    maximum = float(logits.max())
    return maximum + float(np.log(np.exp(logits - maximum).mean())) - float(target @ coefficient)


def _entropy_weights(frame: pd.DataFrame) -> tuple[np.ndarray, list[str], float]:
    design, names = _design(frame)
    treated = frame["event"].to_numpy(dtype=int).astype(bool)
    controls = ~treated
    target = design[treated].mean(axis=0)
    control = design[controls]
    coefficient = np.zeros(control.shape[1], dtype=float)
    gradient_error = float("inf")
    for _ in range(200):
        logits = control @ coefficient
        logits -= logits.max()
        weights = np.exp(logits)
        weights /= weights.sum()
        mean = weights @ control
        gradient = mean - target
        gradient_error = float(np.max(np.abs(gradient)))
        if gradient_error <= 1e-9:
            break
        centered = control - mean
        hessian = (centered * weights[:, None]).T @ centered
        step = np.linalg.solve(hessian + np.eye(hessian.shape[0]) * 1e-8, gradient)
        current = _dual_objective(control, target, coefficient)
        scale = 1.0
        while scale >= 1e-6:
            proposal = coefficient - scale * step
            if _dual_objective(control, target, proposal) < current:
                coefficient = proposal
                break
            scale *= 0.5
        else:
            raise ValueError("entropy balancing line search did not converge")
    if gradient_error > 1e-6:
        raise ValueError(f"entropy balancing did not converge: {gradient_error}")
    logits = control @ coefficient
    logits -= logits.max()
    weights = np.exp(logits)
    weights /= weights.sum()
    return weights, names, gradient_error


def _weighted_ks(treated: np.ndarray, controls: np.ndarray, weights: np.ndarray) -> float:
    points = np.sort(np.unique(np.concatenate([treated, controls])))
    treated_cdf = np.searchsorted(np.sort(treated), points, side="right") / len(treated)
    order = np.argsort(controls)
    control_values = controls[order]
    control_weights = weights[order]
    control_cdf = np.concatenate([[0.0], np.cumsum(control_weights)])[
        np.searchsorted(control_values, points, side="right")
    ]
    return float(np.max(np.abs(treated_cdf - control_cdf)))


def _estimate(frame: pd.DataFrame) -> dict[str, object]:
    weights, _, convergence_error = _entropy_weights(frame)
    treated_mask = frame["event"].eq(1).to_numpy()
    control_mask = ~treated_mask
    treated = frame.loc[treated_mask]
    controls = frame.loc[control_mask]
    balance_rows = []
    for name in CONTINUOUS:
        treated_values = treated[name].to_numpy(dtype=float)
        control_values = controls[name].to_numpy(dtype=float)
        mean_t = float(treated_values.mean())
        mean_c = float(np.average(control_values, weights=weights))
        variance_t = float(np.mean(np.square(treated_values - mean_t)))
        variance_c = float(np.average(np.square(control_values - mean_c), weights=weights))
        pooled = float(np.sqrt((variance_t + variance_c) / 2.0))
        smd = 0.0 if pooled == 0 else (mean_t - mean_c) / pooled
        balance_rows.append({
            "feature": name,
            "treated_mean": mean_t,
            "weighted_control_mean": mean_c,
            "standardized_mean_difference": smd,
        })
    outcome_t = treated["return_1130"].to_numpy(dtype=float)
    outcome_c = controls["return_1130"].to_numpy(dtype=float)
    return {
        "effect": float(outcome_t.mean() - np.average(outcome_c, weights=weights)),
        "raw_event_mean": float(outcome_t.mean()),
        "raw_control_mean": float(outcome_c.mean()),
        "weighted_control_mean": float(np.average(outcome_c, weights=weights)),
        "control_ess": float(1.0 / np.square(weights).sum()),
        "maximum_control_weight": float(weights.max()),
        "prior_return_weighted_ks": _weighted_ks(
            treated["prior_return_1d"].to_numpy(dtype=float),
            controls["prior_return_1d"].to_numpy(dtype=float),
            weights,
        ),
        "convergence_error": convergence_error,
        "balance": pd.DataFrame(balance_rows),
        "control_weights": pd.DataFrame({
            "date": controls.index.strftime("%Y-%m-%d"),
            "weight": weights,
        }),
    }


def _bootstrap(frame: pd.DataFrame, iterations: int, seed: int) -> np.ndarray:
    periods = pd.PeriodIndex(frame.index, freq="M")
    unique = periods.unique().sort_values()
    groups = [np.flatnonzero(periods == period) for period in unique]
    rng = np.random.default_rng(seed)
    results: list[float] = []
    attempts = 0
    while len(results) < iterations:
        attempts += 1
        if attempts > iterations * 3:
            raise ValueError("too many invalid entropy-balance bootstrap samples")
        selected = rng.integers(0, len(groups), size=len(groups))
        sample = frame.iloc[np.concatenate([groups[item] for item in selected])].copy()
        if sample["event"].nunique() != 2:
            continue
        try:
            results.append(float(_estimate(sample)["effect"]))
        except (ValueError, np.linalg.LinAlgError):
            continue
    return np.asarray(results, dtype=float)


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
        raise ValueError("EX65 is diagnostic only")

    dataset = protocol["dataset"]
    ex62 = repo / "experiments/S003/20260911_S003_EX62"
    source = repo / "experiments/S003" / dataset["event_experiment"]
    validate_experiment_archive(ex62)
    validate_experiment_archive(source)
    expected = {
        ex62 / "experiment_manifest.json": dataset["ex62_manifest_sha256"],
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
        subset=[*CONTINUOUS, "return_1130"]
    )

    result = _estimate(frame)
    balance = result.pop("balance")
    control_weights = result.pop("control_weights")
    bootstrap = protocol["bootstrap"]
    draws = _bootstrap(frame, int(bootstrap["iterations"]), int(bootstrap["seed"]))
    lower = float(np.quantile(draws, 0.05))
    upper = float(np.quantile(draws, 0.95))

    limits = protocol["interpretation"]
    prior_smd = float(balance.loc[
        balance["feature"].eq("prior_return_1d"), "standardized_mean_difference"
    ].abs().iloc[0])
    max_smd = float(balance["standardized_mean_difference"].abs().max())
    gates = {
        "prior_return_smd": prior_smd <= float(limits["prior_return_max_absolute_smd"]),
        "continuous_smd": max_smd <= float(limits["continuous_max_absolute_smd"]),
        "prior_return_ks": float(result["prior_return_weighted_ks"]) <= float(
            limits["prior_return_max_weighted_ks"]
        ),
        "control_ess": float(result["control_ess"]) >= float(
            limits["control_minimum_effective_sample_size"]
        ),
        "maximum_control_weight": float(result["maximum_control_weight"]) <= float(
            limits["control_maximum_single_weight"]
        ),
    }
    if not all(gates.values()):
        label = "BALANCE_OR_OVERLAP_FAILED"
    elif lower > 0:
        label = "MONEYFLOW_INCREMENT_SUPPORTED"
    elif upper <= 0:
        label = "MOMENTUM_EXPLANATION_SUPPORTED"
    else:
        label = "INCREMENT_UNRESOLVED"

    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "event_count": int(frame["event"].sum()),
        "non_event_count": int(frame["event"].eq(0).sum()),
        "competing_explanation_label": label,
        "gates": gates,
        "metrics": {
            **result,
            "effect_lower_90": lower,
            "effect_upper_90": upper,
            "effect_positive_probability": float((draws > 0).mean()),
            "prior_return_absolute_smd": prior_smd,
            "continuous_maximum_absolute_smd": max_smd,
        },
        "candidate_changed": False,
        "sm_changed": False,
        "pte_changed": False,
    }
    balance.to_csv(artifacts / "covariate_balance.csv", index=False, lineterminator="\n")
    control_weights.to_csv(artifacts / "control_weights.csv", index=False, lineterminator="\n")
    pd.DataFrame({"effect_11_30": draws}).to_csv(
        artifacts / "bootstrap_effects.csv", index=False, lineterminator="\n"
    )
    _write_json(artifacts / "robustness_summary.json", summary)

    metrics = summary["metrics"]
    (experiment / "03_execution.md").write_text(
        "# S003 EX65 执行\n\n"
        f"熵平衡复核：`PASS`。事件{summary['event_count']}日、非事件"
        f"{summary['non_event_count']}日；前一日收益SMD为"
        f"{metrics['prior_return_absolute_smd']:.4f}，加权KS为"
        f"{metrics['prior_return_weighted_ks']:.4f}，控制组有效样本量"
        f"{metrics['control_ess']:.1f}。调整后事件效应{metrics['effect']:.2%}，"
        f"自然月区块Bootstrap 90%区间[{lower:.2%}, {upper:.2%}]。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX65 结论\n\n"
        f"竞争解释标签：`{label}`。本轮严格平衡前一日收益及其他已观测状态后，"
        f"开盘至11:30事件效应为{metrics['effect']:.2%}，90%区间"
        f"[{lower:.2%}, {upper:.2%}]。结果只用于理解机制，不修改S003-v1。\n",
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
