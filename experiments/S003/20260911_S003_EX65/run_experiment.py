from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import linprog, minimize
from scipy.special import logsumexp

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
    values["prior_return_1d_z2"] = prior.pow(2)
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


def _entropy_weights(frame: pd.DataFrame) -> tuple[np.ndarray, list[str], float]:
    design, names = _design(frame)
    treated = frame["event"].to_numpy(dtype=int).astype(bool)
    controls = ~treated
    target = design[treated].mean(axis=0)
    control = design[controls]
    feasibility = linprog(
        np.zeros(len(control), dtype=float),
        A_eq=np.vstack([np.ones(len(control), dtype=float), control.T]),
        b_eq=np.concatenate([[1.0], target]),
        bounds=(0.0, None),
        method="highs",
    )
    if not feasibility.success:
        raise ValueError(f"entropy balance has no exact common support: {feasibility.message}")
    def objective(coefficient: np.ndarray) -> tuple[float, np.ndarray]:
        logits = control @ coefficient
        weights = np.exp(logits - logsumexp(logits))
        value = float(logsumexp(logits) - np.log(len(control)) - target @ coefficient)
        gradient = control.T @ weights - target
        return value, gradient

    fitted = minimize(
        objective,
        np.zeros(control.shape[1], dtype=float),
        method="L-BFGS-B",
        jac=True,
        options={"maxiter": 2000, "ftol": 1e-14, "gtol": 1e-10, "maxls": 50},
    )
    coefficient = fitted.x
    _, gradient = objective(coefficient)
    gradient_error = float(np.max(np.abs(gradient)))
    if gradient_error > 1e-6:
        raise ValueError(
            f"entropy balancing did not converge: {fitted.message}; {gradient_error}"
        )
    logits = control @ coefficient
    logits -= logits.max()
    weights = np.exp(logits)
    weights /= weights.sum()
    return weights, names, gradient_error


def _support_diagnostics(frame: pd.DataFrame) -> pd.DataFrame:
    design, names = _design(frame)
    treated = frame["event"].to_numpy(dtype=int).astype(bool)
    control = design[~treated]
    target = design[treated].mean(axis=0)
    groups = [
        ("PRIOR_RETURN_MOMENTS", [name.startswith("prior_return_1d_z") for name in names]),
        (
            "PRIOR_RETURN_MOMENTS_AND_DECILES",
            [name.startswith("prior_return_1d_z") or name.startswith("prior_return_bin_") for name in names],
        ),
        ("ALL_PRE_OPEN_STATE", [not name.startswith("year_") for name in names]),
        ("ALL_FEATURES", [True] * len(names)),
    ]
    rows = []
    for label, mask in groups:
        indexes = np.flatnonzero(mask)
        result = linprog(
            np.zeros(len(control), dtype=float),
            A_eq=np.vstack([np.ones(len(control), dtype=float), control[:, indexes].T]),
            b_eq=np.concatenate([[1.0], target[indexes]]),
            bounds=(0.0, None),
            method="highs",
        )
        rows.append({
            "constraint_set": label,
            "constraint_count": int(len(indexes)),
            "exact_common_support": bool(result.success),
            "solver_message": str(result.message),
        })
    return pd.DataFrame(rows)


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

    try:
        result = _estimate(frame)
    except ValueError as exc:
        if "no exact common support" not in str(exc):
            raise
        diagnostics = _support_diagnostics(frame)
        diagnostics.to_csv(artifacts / "common_support_diagnostics.csv", index=False, lineterminator="\n")
        summary = {
            "schema_version": 1,
            "experiment_id": EXPERIMENT_ID,
            "status": "PASS",
            "event_count": int(frame["event"].sum()),
            "non_event_count": int(frame["event"].eq(0).sum()),
            "competing_explanation_label": "BALANCE_OR_OVERLAP_FAILED",
            "gates": {
                "exact_common_support": False,
                "prior_return_smd": False,
                "continuous_smd": False,
                "prior_return_ks": False,
                "control_ess": False,
                "maximum_control_weight": False,
            },
            "metrics": {
                "solver_reason": str(exc),
                "event_prior_return_min": float(frame.loc[frame["event"].eq(1), "prior_return_1d"].min()),
                "event_prior_return_max": float(frame.loc[frame["event"].eq(1), "prior_return_1d"].max()),
                "control_prior_return_min": float(frame.loc[frame["event"].eq(0), "prior_return_1d"].min()),
                "control_prior_return_max": float(frame.loc[frame["event"].eq(0), "prior_return_1d"].max()),
            },
            "candidate_changed": False,
            "sm_changed": False,
            "pte_changed": False,
        }
        _write_json(artifacts / "robustness_summary.json", summary)
        (experiment / "03_execution.md").write_text(
            "# S003 EX65 执行\n\n"
            f"严格熵平衡复核已执行。事件{summary['event_count']}日、非事件"
            f"{summary['non_event_count']}日；完整事件样本不存在满足预注册约束的非负控制权重，"
            "因此不计算调整后收益和Bootstrap区间。\n",
            encoding="utf-8",
        )
        (experiment / "04_conclusion.md").write_text(
            "# S003 EX65 结论\n\n"
            "竞争解释标签：`BALANCE_OR_OVERLAP_FAILED`。完整事件总体与非事件总体缺少严格共同"
            "支持，现有开发池无法在保留全部事件的同时分离资金流宽度与短期动量。该结果不证明"
            "资金流增量不存在，也不支持其已经独立；下一步只能在预先定义的共同支持子样本内估计。"
            "结果不修改S003-v1。\n",
            encoding="utf-8",
        )
        build_experiment_manifest(experiment, {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "release_id": "S003-v1",
            "symbol": "510500.SH",
            "development_cutoff": str(cutoff.date()),
            "status": "PASS",
            "competing_explanation_label": "BALANCE_OR_OVERLAP_FAILED",
            "candidate_generation": False,
            "mutates_strategy_manager": False,
            "mutates_pte": False,
        })
        validate_experiment_archive(experiment)
        return
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
