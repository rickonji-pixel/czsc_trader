from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

import numpy as np
import optuna
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.search import (
    ConstraintSpec,
    FloatParameter,
    ObjectiveSpec,
    SearchEvaluation,
    SearchSpec,
    SearchTrialRejected,
    run_search,
)


EXPERIMENT_ID = "20260915_S007_EX09"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _raw_daily(repo: Path, hashes: dict[str, str]) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for relative, expected in hashes.items():
        path = repo / relative
        if _sha256(path) != expected:
            raise ValueError(f"daily source differs from search protocol: {relative}")
        rows.append(pd.read_csv(path))
    frame = pd.concat(rows, ignore_index=True)
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    return frame.sort_values("date").drop_duplicates("date", keep="last").set_index("date")


def _causal_percentile(values: pd.Series, window: int, minimum: int) -> pd.Series:
    def rank_last(items: np.ndarray) -> float:
        current = items[-1]
        valid = items[np.isfinite(items)]
        if not np.isfinite(current) or len(valid) < minimum:
            return np.nan
        return float((np.count_nonzero(valid < current) + 0.5 * np.count_nonzero(valid == current)) / len(valid) - 0.5)

    return values.astype(float).rolling(window, min_periods=minimum).apply(rank_last, raw=True)


def _hysteresis(score: pd.Series, entry: float, exit_: float) -> pd.Series:
    current = 0.0
    output = np.zeros(len(score), dtype=float)
    for index, value in enumerate(score.to_numpy(dtype=float)):
        if np.isfinite(value):
            if current == 0.0 and value >= entry:
                current = 1.0
            elif current == 1.0 and value <= exit_:
                current = 0.0
        output[index] = current
    return pd.Series(output, index=score.index, name="decision_target")


def _metrics(execution_target: pd.Series, prices: pd.DataFrame, fee: float) -> dict[str, float | int]:
    target = execution_target.reindex(prices.index).fillna(0.0).astype(float)
    if not target.isin([0.0, 1.0]).all():
        raise ValueError("execution target must be binary")
    cash = 1.0
    shares = 0.0
    previous = 0.0
    values = np.empty(len(prices), dtype=float)
    opens = prices["open"].astype(float).to_numpy()
    closes = prices["close"].astype(float).to_numpy()
    flags = target.to_numpy(dtype=float)
    order_count = 0
    for index, (desired, open_price, close_price) in enumerate(zip(flags, opens, closes, strict=True)):
        if desired != previous:
            if desired == 1.0:
                shares = cash / (open_price * (1.0 + fee))
                cash = 0.0
            else:
                cash = shares * open_price * (1.0 - fee)
                shares = 0.0
            order_count += 1
            previous = desired
        values[index] = cash + shares * close_price
    peak = np.maximum.accumulate(np.concatenate(([1.0], values)))[1:]
    maximum_drawdown = float(np.min(values / peak - 1.0))
    years = max(len(prices) / 252.0, 1.0 / 252.0)
    cagr = float(values[-1] ** (1.0 / years) - 1.0)
    exits = target.shift(1, fill_value=0.0).gt(0) & target.eq(0)
    rolling = exits.astype(int).rolling(60, min_periods=60).sum().dropna()
    return {
        "cagr": cagr,
        "total_return": float(values[-1] - 1.0),
        "maximum_drawdown": maximum_drawdown,
        "calmar": cagr / abs(maximum_drawdown) if maximum_drawdown < 0 else 100.0,
        "closed_trades": int(exits.sum()),
        "order_count": int(order_count),
        "rolling_60_closed_trades_median": float(rolling.median()) if len(rolling) else 0.0,
        "rolling_60_closed_trades_p10": float(rolling.quantile(0.10)) if len(rolling) else 0.0,
        "exposure_ratio": float(target.mean()),
    }


def _pareto(frame: pd.DataFrame, columns: list[str]) -> pd.Series:
    values = frame[columns].to_numpy(dtype=float)
    keep = np.ones(len(frame), dtype=bool)
    for index, current in enumerate(values):
        dominates = np.all(values >= current, axis=1) & np.any(values > current, axis=1)
        dominates[index] = False
        if dominates.any():
            keep[index] = False
    return pd.Series(keep, index=frame.index)


def main() -> None:
    started = time.perf_counter()
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_new_returns"):
        raise ValueError("EX09 protocol identity or return declaration differs")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX09 cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    ex08 = repo / str(sources["ex08_archive"])
    validate_experiment_archive(ex08)
    frozen = {
        ex08 / "experiment_manifest.json": sources["ex08_manifest_sha256"],
        ex08 / "artifacts/effective_search_protocol.json": sources["effective_search_protocol_sha256"],
        repo / str(sources["feature_panel"]): sources["feature_panel_sha256"],
        repo / "src/czsc_trader/search/optuna_adapter.py": sources["search_adapter_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen search source differs: {path}")
    effective = _read(ex08 / "artifacts/effective_search_protocol.json")
    if effective["search"]["storage"] != "IN_MEMORY" or effective["search"]["method"] != "nsga2":
        raise ValueError("effective search method or storage differs")

    panel = pd.read_csv(repo / str(sources["feature_panel"]), parse_dates=["date"]).set_index("date")
    prices = _raw_daily(repo, sources["daily_sha256"])
    prices = prices.loc[prices.index <= pd.Timestamp(protocol["development_cutoff"])]
    if not panel.index.equals(prices.index):
        raise ValueError("feature panel and price calendar differ")
    bindings = effective["feature_bindings"]
    scores = pd.DataFrame(index=panel.index)
    normalization = effective["normalization"]
    for feature, binding in bindings.items():
        scores[feature] = _causal_percentile(
            panel[feature],
            int(normalization["lookback_sessions"]),
            int(normalization["minimum_observations"]),
        ) * int(binding["orientation"])

    segments = protocol["segments"]
    discovery_mask = scores.index.to_series().between(segments["discovery_start"], segments["discovery_end"])
    confirmation_mask = scores.index.to_series().between(segments["confirmation_start"], segments["confirmation_end"])
    execution = effective["execution"]
    gates = effective["hard_gates"]
    fee = float(execution["fee_rate_one_way"])
    search = effective["search"]
    parameter_bounds = search["parameters"]

    features = {
        "opp_spx": "risk_global_spx_return",
        "opp_chinext": "risk_chinext_turnover_z20",
        "confirm_share": "micro_share_change_5d_lag1",
        "confirm_volume": "tsfresh__log_volume_change__mean__lb20",
        "entry_vwap": "price_close_vwap_deviation",
        "risk_shibor": "risk_shibor_on_change_5d",
        "risk_range": "price_intraday_range",
    }

    def evaluator(params: dict[str, object]) -> SearchEvaluation:
        opportunity = float(params["opportunity_total"])
        confirmation = float(params["confirmation_total"])
        entry_timing = float(params["entry_timing_total"])
        risk_context = 1.0 - opportunity - confirmation - entry_timing
        low_risk, high_risk = map(float, parameter_bounds["risk_context_remainder"])
        if not low_risk <= risk_context <= high_risk:
            raise SearchTrialRejected("RISK_WEIGHT_OUT_OF_RANGE", "risk-context remainder is outside preregistered bounds")
        entry_quantile = float(params["entry_quantile"])
        exit_quantile = float(params["exit_quantile"])
        if exit_quantile > entry_quantile - float(parameter_bounds["minimum_quantile_gap"]):
            raise SearchTrialRejected("THRESHOLD_GAP_TOO_SMALL", "exit quantile must be sufficiently below entry quantile")

        opp_fraction = float(params["opp_spx_fraction"])
        confirm_fraction = float(params["confirm_share_fraction"])
        risk_fraction = float(params["risk_shibor_fraction"])
        weights = {
            features["opp_spx"]: opportunity * opp_fraction,
            features["opp_chinext"]: opportunity * (1.0 - opp_fraction),
            features["confirm_share"]: confirmation * confirm_fraction,
            features["confirm_volume"]: confirmation * (1.0 - confirm_fraction),
            features["entry_vwap"]: entry_timing,
            features["risk_shibor"]: risk_context * risk_fraction,
            features["risk_range"]: risk_context * (1.0 - risk_fraction),
        }
        if max(weights.values()) > float(parameter_bounds["maximum_single_factor_weight"]):
            raise SearchTrialRejected("FACTOR_WEIGHT_TOO_LARGE", "single-factor weight exceeds preregistered limit")
        combined = sum(scores[name] * weight for name, weight in weights.items()).where(scores.notna().all(axis=1))
        discovery_values = combined.loc[discovery_mask].dropna()
        if len(discovery_values) < 300:
            raise SearchTrialRejected("INSUFFICIENT_DISCOVERY_SCORE", "too few discovery scores")
        entry_threshold = float(discovery_values.quantile(entry_quantile))
        exit_threshold = float(discovery_values.quantile(exit_quantile))
        if exit_threshold >= entry_threshold:
            raise SearchTrialRejected("INVALID_THRESHOLDS", "numeric exit threshold is not below entry threshold")
        decision = _hysteresis(combined, entry_threshold, exit_threshold)
        execution_target = decision.shift(1).fillna(0.0)
        full = _metrics(execution_target, prices, fee)
        discovery_metrics = _metrics(execution_target.loc[discovery_mask], prices.loc[discovery_mask], fee)
        confirmation_metrics = _metrics(execution_target.loc[confirmation_mask], prices.loc[confirmation_mask], fee)
        payload = {
            "params": params,
            "weights": weights,
            "entry_threshold": entry_threshold,
            "exit_threshold": exit_threshold,
        }
        strategy_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        behavior_hash = hashlib.sha256(execution_target.to_numpy(dtype=np.int8).tobytes()).hexdigest()
        return SearchEvaluation(
            objectives={
                "cagr": float(full["cagr"]),
                "max_drawdown": float(full["maximum_drawdown"]),
                "calmar": float(full["calmar"]),
            },
            constraints={
                "full_cagr_shortfall": float(gates["minimum_full_cagr"]) - float(full["cagr"]),
                "full_drawdown_loss": abs(min(float(full["maximum_drawdown"]), 0.0)),
                "s001_drawdown_loss": abs(min(float(full["maximum_drawdown"]), 0.0)),
                "frequency_median_shortfall": float(gates["minimum_rolling_60_closed_trades_median"]) - float(full["rolling_60_closed_trades_median"]),
                "frequency_p10_shortfall": float(gates["minimum_rolling_60_closed_trades_p10"]) - float(full["rolling_60_closed_trades_p10"]),
                "discovery_cagr_shortfall": float(gates["minimum_discovery_cagr"]) - float(discovery_metrics["cagr"]),
                "confirmation_cagr_shortfall": float(gates["minimum_confirmation_cagr"]) - float(confirmation_metrics["cagr"]),
                "confirmation_drawdown_loss": abs(min(float(confirmation_metrics["maximum_drawdown"]), 0.0)),
            },
            strategy_hash=strategy_hash,
            behavior_hash=behavior_hash,
            metadata={
                "weights": weights,
                "entry_threshold": entry_threshold,
                "exit_threshold": exit_threshold,
                "full": full,
                "discovery": discovery_metrics,
                "confirmation": confirmation_metrics,
            },
        )

    low_fraction, high_fraction = map(float, parameter_bounds["within_role_fraction"])
    spec = SearchSpec(
        study_name="S007-EX09-WEIGHTED-SCORE",
        method="nsga2",
        parameters=(
            FloatParameter("opportunity_total", *map(float, parameter_bounds["opportunity_total"])),
            FloatParameter("confirmation_total", *map(float, parameter_bounds["confirmation_total"])),
            FloatParameter("entry_timing_total", *map(float, parameter_bounds["entry_timing_total"])),
            FloatParameter("opp_spx_fraction", low_fraction, high_fraction),
            FloatParameter("confirm_share_fraction", low_fraction, high_fraction),
            FloatParameter("risk_shibor_fraction", low_fraction, high_fraction),
            FloatParameter("entry_quantile", *map(float, parameter_bounds["entry_quantile"])),
            FloatParameter("exit_quantile", *map(float, parameter_bounds["exit_quantile"])),
        ),
        objectives=(
            ObjectiveSpec("cagr", "maximize"),
            ObjectiveSpec("max_drawdown", "maximize"),
            ObjectiveSpec("calmar", "maximize"),
        ),
        constraints=(
            ConstraintSpec("full_cagr_shortfall", 0.0),
            ConstraintSpec("full_drawdown_loss", abs(float(gates["maximum_drawdown_limit"]))),
            ConstraintSpec("s001_drawdown_loss", abs(float(gates["s001_v2_maximum_drawdown"]))),
            ConstraintSpec("frequency_median_shortfall", 0.0),
            ConstraintSpec("frequency_p10_shortfall", 0.0),
            ConstraintSpec("discovery_cagr_shortfall", 0.0),
            ConstraintSpec("confirmation_cagr_shortfall", 0.0),
            ConstraintSpec("confirmation_drawdown_loss", abs(float(gates["confirmation_maximum_drawdown_limit"]))),
        ),
        target_trials=int(search["target_trials"]),
        seed=int(search["seed"]),
        storage_path=None,
        workers=int(search["workers"]),
    )
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    result = run_search(spec, evaluator)
    ledger = result.ledger.copy()
    metadata = pd.json_normalize(ledger["metadata"].map(json.loads)).add_prefix("metric.")
    enriched = pd.concat([ledger, metadata], axis=1)
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    enriched.to_csv(artifacts / "search_trial_ledger.csv.gz", index=False, compression=compression, lineterminator="\n")
    feasible = enriched.loc[enriched["state"].eq("COMPLETE") & enriched["feasible"].eq(True)].copy()
    if len(feasible):
        feasible["pareto"] = _pareto(feasible, ["objective.cagr", "objective.max_drawdown", "objective.calmar"])
        pareto = feasible.loc[feasible["pareto"]].copy()
    else:
        pareto = feasible.copy()
    feasible.to_csv(artifacts / "feasible_trials.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    pareto.to_csv(artifacts / "pareto_trials.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    buyhold = _metrics(pd.Series(1.0, index=prices.index), prices, fee)
    completed = enriched.loc[enriched["state"].eq("COMPLETE")].sort_values("objective.cagr", ascending=False)
    best = completed.iloc[0]
    decision = "PROCEED_TO_PARAMETER_PLATFORM_AUDIT" if len(feasible) else "STOP_WEIGHTED_SCORE_NO_FEASIBLE_TRIAL"
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": decision,
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "search_outcome": result.outcome,
        "contract_digest": result.contract_digest,
        "target_trials": result.target_trials,
        "completed_trials": result.completed_trials,
        "pruned_trials": result.pruned_trials,
        "failed_trials": result.failed_trials,
        "feasible_trials": result.feasible_trials,
        "pareto_trials": int(len(pareto)),
        "best_completed": {
            "trial_id": str(best["trial_id"]),
            "cagr": float(best["objective.cagr"]),
            "maximum_drawdown": float(best["objective.max_drawdown"]),
            "calmar": float(best["objective.calmar"]),
            "frequency_median": float(best["metric.full.rolling_60_closed_trades_median"]),
            "frequency_p10": float(best["metric.full.rolling_60_closed_trades_p10"]),
        },
        "buyhold": buyhold,
        "search_trials_added_to_multiplicity_ledger": int(result.completed_trials + result.pruned_trials),
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "search_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        f"# S007 EX09 执行\n\n状态：`COMPLETE`。Optuna完成{result.completed_trials}个有效评估，结构剪枝{result.pruned_trials}个，失败{result.failed_trials}个；满足全部硬门{result.feasible_trials}个。完整trial ledger已归档。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        f"# S007 EX09 结论\n\n裁决：`{decision}`。最佳已完成试验年化{float(best['objective.cagr']):.2%}、最大回撤{float(best['objective.max_drawdown']):.2%}、卡玛{float(best['objective.calmar']):.2f}，60日闭合交易中位数/P10为{float(best['metric.full.rolling_60_closed_trades_median']):.1f}/{float(best['metric.full.rolling_60_closed_trades_p10']):.1f}。本轮只搜索参数，未选冠军或创建候选。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {"experiment_id": EXPERIMENT_ID, "status": "COMPLETE", "experiment_type": protocol["experiment_type"], "strategy_id": protocol["strategy_id"], "symbol": protocol["symbol"], "development_cutoff": protocol["development_cutoff"], "decision": decision, "promotion_allowed": False})
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
