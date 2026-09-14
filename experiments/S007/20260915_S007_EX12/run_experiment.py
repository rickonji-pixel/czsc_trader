from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX12"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_helpers(path: Path):
    spec = importlib.util.spec_from_file_location("s007_ex09_helpers", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load EX09 helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _daily_returns(target: pd.Series, prices: pd.DataFrame, fee: float) -> pd.Series:
    desired = target.reindex(prices.index).fillna(0.0).to_numpy(dtype=float)
    opens = prices["open"].to_numpy(dtype=float)
    closes = prices["close"].to_numpy(dtype=float)
    cash = 1.0
    shares = 0.0
    previous = 0.0
    values = np.empty(len(prices), dtype=float)
    for index, (flag, open_price, close_price) in enumerate(zip(desired, opens, closes, strict=True)):
        if flag != previous:
            if flag == 1.0:
                shares = cash / (open_price * (1.0 + fee))
                cash = 0.0
            else:
                cash = shares * open_price * (1.0 - fee)
                shares = 0.0
            previous = flag
        values[index] = cash + shares * close_price
    previous_values = np.concatenate(([1.0], values[:-1]))
    return pd.Series(values / previous_values - 1.0, index=prices.index)


def _circular_block_bootstrap(values: np.ndarray, block: int, repetitions: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    size = len(values)
    blocks = int(np.ceil(size / block))
    results = np.empty(repetitions, dtype=float)
    offsets = np.arange(block)
    for repetition in range(repetitions):
        starts = rng.integers(0, size, size=blocks)
        indices = ((starts[:, None] + offsets[None, :]) % size).reshape(-1)[:size]
        results[repetition] = float(values[indices].mean() * 252.0)
    return results


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_new_returns"):
        raise ValueError("EX12 protocol identity or return declaration differs")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX12 cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    ex11 = repo / str(sources["ex11_archive"])
    ex09 = repo / str(sources["ex09_archive"])
    validate_experiment_archive(ex11)
    validate_experiment_archive(ex09)
    frozen = {
        ex11 / "experiment_manifest.json": sources["ex11_manifest_sha256"],
        ex11 / "artifacts/temporal_diagnosis.json": sources["temporal_diagnosis_sha256"],
        ex11 / "artifacts/temporal_performance.csv": sources["temporal_performance_sha256"],
        ex09 / "artifacts/feasible_trials.csv": sources["feasible_trials_sha256"],
        ex09 / "run_experiment.py": sources["ex09_script_sha256"],
        repo / str(sources["feature_panel"]): sources["feature_panel_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen robustness source differs: {path}")

    diagnosis = _read(ex11 / "artifacts/temporal_diagnosis.json")
    medoid_id = str(diagnosis["medoid_trial_id"])
    feasible = pd.read_csv(ex09 / "artifacts/feasible_trials.csv")
    matches = feasible.loc[feasible["trial_id"].eq(medoid_id)]
    if len(matches) != 1:
        raise ValueError("EX11 medoid trial is not unique in EX09 ledger")
    medoid = matches.iloc[0]
    metadata = json.loads(medoid["metadata"])
    params = json.loads(medoid["params"])
    helpers = _load_helpers(ex09 / "run_experiment.py")
    search_protocol = _read(ex09 / "artifacts/protocol.json")
    ex08 = repo / str(search_protocol["sources"]["ex08_archive"])
    effective = _read(ex08 / "artifacts/effective_search_protocol.json")
    panel = pd.read_csv(repo / str(sources["feature_panel"]), parse_dates=["date"]).set_index("date")
    prices = helpers._raw_daily(repo, search_protocol["sources"]["daily_sha256"])
    prices = prices.loc[prices.index <= pd.Timestamp(protocol["development_cutoff"])]
    if not panel.index.equals(prices.index):
        raise ValueError("feature panel and price calendar differ")
    normalization = effective["normalization"]
    oriented = pd.DataFrame(index=panel.index)
    for feature, binding in effective["feature_bindings"].items():
        oriented[feature] = helpers._causal_percentile(
            panel[feature],
            int(normalization["lookback_sessions"]),
            int(normalization["minimum_observations"]),
        ) * int(binding["orientation"])

    weights = {name: float(value) for name, value in metadata["weights"].items()}
    combined = sum(oriented[name] * weight for name, weight in weights.items()).where(oriented.notna().all(axis=1))
    base_decision = helpers._hysteresis(combined, float(metadata["entry_threshold"]), float(metadata["exit_threshold"]))
    base_target = base_decision.shift(1).fillna(0.0)
    gates = protocol["hard_gates"]

    cost_rows: list[dict[str, object]] = []
    for fee in protocol["cost_stress_fee_rates_one_way"]:
        metrics = helpers._metrics(base_target, prices, float(fee))
        cost_rows.append({"fee_rate_one_way": float(fee), **metrics})
    costs = pd.DataFrame(cost_rows)
    costs.to_csv(artifacts / "cost_stress.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    base_metrics = helpers._metrics(base_target, prices, float(protocol["cost_stress_fee_rates_one_way"][0]))
    discovery_mask = panel.index.to_series().between("2021-01-04", "2023-12-31")
    ablation_rows: list[dict[str, object]] = []
    for removed in sorted(weights):
        remaining = {name: weight for name, weight in weights.items() if name != removed}
        total = sum(remaining.values())
        remaining = {name: weight / total for name, weight in remaining.items()}
        score = sum(oriented[name] * weight for name, weight in remaining.items()).where(
            oriented[list(remaining)].notna().all(axis=1)
        )
        discovery = score.loc[discovery_mask].dropna()
        entry = float(discovery.quantile(float(params["entry_quantile"])))
        exit_ = float(discovery.quantile(float(params["exit_quantile"])))
        target = helpers._hysteresis(score, entry, exit_).shift(1).fillna(0.0)
        metrics = helpers._metrics(target, prices, float(protocol["cost_stress_fee_rates_one_way"][0]))
        frequency_pass = (
            float(metrics["rolling_60_closed_trades_median"]) >= float(gates["minimum_rolling_60_closed_trades_median"])
            and float(metrics["rolling_60_closed_trades_p10"]) >= float(gates["minimum_rolling_60_closed_trades_p10"])
        )
        dominates = (
            float(metrics["cagr"]) >= float(base_metrics["cagr"])
            and float(metrics["maximum_drawdown"]) >= float(base_metrics["maximum_drawdown"])
            and (
                float(metrics["cagr"]) > float(base_metrics["cagr"]) + 1e-12
                or float(metrics["maximum_drawdown"]) > float(base_metrics["maximum_drawdown"]) + 1e-12
            )
            and frequency_pass
        )
        ablation_rows.append({
            "removed_factor": removed,
            "removed_weight": weights[removed],
            "entry_threshold": entry,
            "exit_threshold": exit_,
            "dominates_base": bool(dominates),
            **metrics,
        })
    ablations = pd.DataFrame(ablation_rows).sort_values(["dominates_base", "cagr"], ascending=False)
    ablations.to_csv(artifacts / "single_factor_ablations.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    temporal = pd.read_csv(ex11 / "artifacts/temporal_performance.csv")
    annual = temporal.loc[temporal["period"].str.startswith("YEAR_")].pivot(index="period", columns="subject", values="total_return")
    annual["relative_total_return"] = annual["S007_PLATFORM_MEDOID"] - annual["BUYHOLD"]
    annual.reset_index().to_csv(artifacts / "annual_relative_returns.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    positive_relative_years = int(annual["relative_total_return"].gt(0.0).sum())

    base_daily = _daily_returns(base_target, prices, float(protocol["cost_stress_fee_rates_one_way"][0]))
    buyhold_daily = _daily_returns(pd.Series(1.0, index=prices.index), prices, float(protocol["cost_stress_fee_rates_one_way"][0]))
    excess_log = np.log1p(base_daily.to_numpy()) - np.log1p(buyhold_daily.to_numpy())
    bootstrap = protocol["bootstrap"]
    samples = _circular_block_bootstrap(
        excess_log,
        int(bootstrap["block_sessions"]),
        int(bootstrap["repetitions"]),
        int(bootstrap["seed"]),
    )
    alpha = 1.0 - float(bootstrap["confidence_interval"])
    bootstrap_evidence = {
        "repetitions": int(len(samples)),
        "block_sessions": int(bootstrap["block_sessions"]),
        "positive_annualized_log_excess_probability": float(np.mean(samples > 0.0)),
        "annualized_log_excess_mean": float(np.mean(samples)),
        "annualized_log_excess_ci_lower": float(np.quantile(samples, alpha / 2.0)),
        "annualized_log_excess_ci_upper": float(np.quantile(samples, 1.0 - alpha / 2.0)),
    }
    _write(artifacts / "paired_block_bootstrap.json", bootstrap_evidence)

    stress = costs.loc[costs["fee_rate_one_way"].eq(float(gates["maximum_stress_fee_rate_one_way"]))].iloc[0]
    stress_pass = (
        float(stress["cagr"]) >= float(gates["minimum_cagr"])
        and abs(min(float(stress["maximum_drawdown"]), 0.0)) <= float(gates["maximum_drawdown_loss"])
        and float(stress["rolling_60_closed_trades_median"]) >= float(gates["minimum_rolling_60_closed_trades_median"])
        and float(stress["rolling_60_closed_trades_p10"]) >= float(gates["minimum_rolling_60_closed_trades_p10"])
    )
    dominating_ablations = int(ablations["dominates_base"].sum())
    annual_pass = positive_relative_years >= int(gates["minimum_positive_relative_years"])
    bootstrap_pass = (
        float(bootstrap_evidence["positive_annualized_log_excess_probability"])
        >= float(gates["minimum_bootstrap_positive_excess_probability"])
    )
    simplicity_pass = dominating_ablations <= int(gates["maximum_dominating_single_factor_ablations"])
    decision = (
        "PROCEED_TO_CANDIDATE_FORMATION_AND_SE_AUDIT"
        if stress_pass and annual_pass and bootstrap_pass and simplicity_pass
        else "CONTINUE_CONFIGURATION_SIMPLIFICATION"
        if not simplicity_pass
        else "CONTINUE_MECHANISM_RESEARCH"
    )
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": decision,
        "medoid_trial_id": medoid_id,
        "maximum_cost_stress_pass": bool(stress_pass),
        "maximum_cost_stress_metrics": {
            "cagr": float(stress["cagr"]),
            "maximum_drawdown": float(stress["maximum_drawdown"]),
            "rolling_60_closed_trades_median": float(stress["rolling_60_closed_trades_median"]),
            "rolling_60_closed_trades_p10": float(stress["rolling_60_closed_trades_p10"]),
        },
        "positive_relative_years": positive_relative_years,
        "annual_consistency_pass": bool(annual_pass),
        "bootstrap_positive_excess_probability": float(bootstrap_evidence["positive_annualized_log_excess_probability"]),
        "bootstrap_pass": bool(bootstrap_pass),
        "dominating_single_factor_ablations": dominating_ablations,
        "simplicity_pass": bool(simplicity_pass),
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "robustness_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S007 EX12 执行\n\n"
        f"状态：`COMPLETE`。15bp单边费率下年化{float(stress['cagr']):.2%}、最大回撤"
        f"{float(stress['maximum_drawdown']):.2%}；六年中{positive_relative_years}年跑赢BuyHold；"
        f"区块Bootstrap正超额概率{float(bootstrap_evidence['positive_annualized_log_excess_probability']):.2%}；"
        f"发现{dominating_ablations}个支配原配置的单因子剔除方案。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX12 结论\n\n"
        f"裁决：`{decision}`。成本、年度一致性和配对区块重采样"
        f"{'通过' if stress_pass and annual_pass and bootstrap_pass else '未全部通过'}预注册门槛；"
        f"七因子结构{'未发现可无损删除的组件' if simplicity_pass else '存在应先删除的冗余组件'}。"
        "通过本轮也只获得进入候选形成与SE体检的资格，不形成冻结结论。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": protocol["experiment_type"],
        "strategy_id": protocol["strategy_id"],
        "symbol": protocol["symbol"],
        "development_cutoff": protocol["development_cutoff"],
        "decision": decision,
        "promotion_allowed": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
