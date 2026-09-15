from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import time

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


EXPERIMENT_ID = "20260915_S007_EX14"


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


def _pareto(frame: pd.DataFrame, columns: list[str]) -> pd.Series:
    values = frame[columns].to_numpy(dtype=float)
    keep = pd.Series(True, index=frame.index)
    for offset, index in enumerate(frame.index):
        current = values[offset]
        dominates = (values >= current).all(axis=1) & (values > current).any(axis=1)
        dominates[offset] = False
        if dominates.any():
            keep.loc[index] = False
    return keep


def main() -> None:
    started = time.perf_counter()
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_new_returns"):
        raise ValueError("EX14 protocol identity or return declaration differs")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX14 cannot create, promote, or deploy a candidate")
    cost = protocol["cost_policy"]
    if (
        float(cost["fee_rate_one_way"]) != 0.001
        or float(cost["fixed_order_fee"]) != 0.0
        or float(cost["initial_equity"]) != 1.0
        or bool(cost["fee_is_search_dimension"])
        or not bool(cost["channel_neutral"])
    ):
        raise ValueError("EX14 cost policy differs from preregistered channel-neutral 10bp policy")

    sources = protocol["sources"]
    ex13 = repo / str(sources["ex13_archive"])
    ex08 = repo / str(sources["ex08_archive"])
    ex09_script = repo / "experiments/S007/20260915_S007_EX09/run_experiment.py"
    validate_experiment_archive(ex13)
    validate_experiment_archive(ex08)
    frozen = {
        ex13 / "experiment_manifest.json": sources["ex13_manifest_sha256"],
        ex08 / "experiment_manifest.json": sources["ex08_manifest_sha256"],
        ex08 / "artifacts/effective_search_protocol.json": sources["effective_search_protocol_sha256"],
        repo / str(sources["feature_panel"]): sources["feature_panel_sha256"],
        repo / "src/czsc_trader/search/optuna_adapter.py": sources["search_adapter_sha256"],
        ex09_script: sources["ex09_script_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen cost-aware source differs: {path}")

    helpers = _load_helpers(ex09_script)
    effective = _read(ex08 / "artifacts/effective_search_protocol.json")
    panel = pd.read_csv(repo / str(sources["feature_panel"]), parse_dates=["date"]).set_index("date")
    prices = helpers._raw_daily(repo, sources["daily_sha256"])
    prices = prices.loc[prices.index <= pd.Timestamp(protocol["development_cutoff"])]
    if not panel.index.equals(prices.index):
        raise ValueError("feature panel and price calendar differ")
    normalization = effective["normalization"]
    scores = pd.DataFrame(index=panel.index)
    for feature, binding in effective["feature_bindings"].items():
        scores[feature] = helpers._causal_percentile(
            panel[feature],
            int(normalization["lookback_sessions"]),
            int(normalization["minimum_observations"]),
        ) * int(binding["orientation"])

    segments = protocol["segments"]
    discovery_mask = scores.index.to_series().between(segments["discovery_start"], segments["discovery_end"])
    confirmation_mask = scores.index.to_series().between(segments["confirmation_start"], segments["confirmation_end"])
    fee = float(cost["fee_rate_one_way"])
    gates = effective["hard_gates"]
    search = effective["search"]
    bounds = search["parameters"]
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
        low_risk, high_risk = map(float, bounds["risk_context_remainder"])
        if not low_risk <= risk_context <= high_risk:
            raise SearchTrialRejected("RISK_WEIGHT_OUT_OF_RANGE", "risk-context remainder is outside bounds")
        entry_quantile = float(params["entry_quantile"])
        exit_quantile = float(params["exit_quantile"])
        if exit_quantile > entry_quantile - float(bounds["minimum_quantile_gap"]):
            raise SearchTrialRejected("THRESHOLD_GAP_TOO_SMALL", "exit quantile is too close to entry quantile")
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
        if max(weights.values()) > float(bounds["maximum_single_factor_weight"]):
            raise SearchTrialRejected("FACTOR_WEIGHT_TOO_LARGE", "single-factor weight exceeds limit")
        combined = sum(scores[name] * weight for name, weight in weights.items()).where(scores.notna().all(axis=1))
        discovery_values = combined.loc[discovery_mask].dropna()
        if len(discovery_values) < 300:
            raise SearchTrialRejected("INSUFFICIENT_DISCOVERY_SCORE", "too few discovery scores")
        entry_threshold = float(discovery_values.quantile(entry_quantile))
        exit_threshold = float(discovery_values.quantile(exit_quantile))
        if exit_threshold >= entry_threshold:
            raise SearchTrialRejected("INVALID_THRESHOLDS", "exit threshold is not below entry threshold")
        decision = helpers._hysteresis(combined, entry_threshold, exit_threshold)
        target = decision.shift(1).fillna(0.0)
        full = helpers._metrics(target, prices, fee)
        discovery = helpers._metrics(target.loc[discovery_mask], prices.loc[discovery_mask], fee)
        confirmation_metrics = helpers._metrics(target.loc[confirmation_mask], prices.loc[confirmation_mask], fee)
        payload = {"params": params, "weights": weights, "entry_threshold": entry_threshold, "exit_threshold": exit_threshold}
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
                "discovery_cagr_shortfall": float(gates["minimum_discovery_cagr"]) - float(discovery["cagr"]),
                "confirmation_cagr_shortfall": float(gates["minimum_confirmation_cagr"]) - float(confirmation_metrics["cagr"]),
                "confirmation_drawdown_loss": abs(min(float(confirmation_metrics["maximum_drawdown"]), 0.0)),
            },
            strategy_hash=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
            behavior_hash=hashlib.sha256(target.to_numpy(dtype="int8").tobytes()).hexdigest(),
            metadata={
                "weights": weights,
                "entry_threshold": entry_threshold,
                "exit_threshold": exit_threshold,
                "fee_rate_one_way": fee,
                "full": full,
                "discovery": discovery,
                "confirmation": confirmation_metrics,
            },
        )

    low_fraction, high_fraction = map(float, bounds["within_role_fraction"])
    spec = SearchSpec(
        study_name="S007-EX14-COST-AWARE-WEIGHTED-SCORE",
        method="nsga2",
        parameters=(
            FloatParameter("opportunity_total", *map(float, bounds["opportunity_total"])),
            FloatParameter("confirmation_total", *map(float, bounds["confirmation_total"])),
            FloatParameter("entry_timing_total", *map(float, bounds["entry_timing_total"])),
            FloatParameter("opp_spx_fraction", low_fraction, high_fraction),
            FloatParameter("confirm_share_fraction", low_fraction, high_fraction),
            FloatParameter("risk_shibor_fraction", low_fraction, high_fraction),
            FloatParameter("entry_quantile", *map(float, bounds["entry_quantile"])),
            FloatParameter("exit_quantile", *map(float, bounds["exit_quantile"])),
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
    completed = enriched.loc[enriched["state"].eq("COMPLETE")].sort_values("objective.cagr", ascending=False)
    best = completed.iloc[0]
    best_feasible = feasible.sort_values(["objective.cagr", "objective.calmar"], ascending=False).iloc[0] if len(feasible) else None
    decision = "PROCEED_TO_10BP_PARAMETER_PLATFORM_AUDIT" if len(feasible) else "STOP_10BP_SEARCH_NO_FEASIBLE_TRIAL"
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": decision,
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "fee_rate_one_way": fee,
        "target_trials": result.target_trials,
        "completed_trials": result.completed_trials,
        "pruned_trials": result.pruned_trials,
        "failed_trials": result.failed_trials,
        "feasible_trials": result.feasible_trials,
        "pareto_trials": int(len(pareto)),
        "unique_feasible_behaviors": int(feasible["behavior_hash"].nunique()) if len(feasible) else 0,
        "best_completed": {
            "trial_id": str(best["trial_id"]),
            "cagr": float(best["objective.cagr"]),
            "maximum_drawdown": float(best["objective.max_drawdown"]),
            "calmar": float(best["objective.calmar"]),
            "frequency_median": float(best["metric.full.rolling_60_closed_trades_median"]),
            "frequency_p10": float(best["metric.full.rolling_60_closed_trades_p10"]),
        },
        "best_feasible": None if best_feasible is None else {
            "trial_id": str(best_feasible["trial_id"]),
            "cagr": float(best_feasible["objective.cagr"]),
            "maximum_drawdown": float(best_feasible["objective.max_drawdown"]),
            "calmar": float(best_feasible["objective.calmar"]),
            "frequency_median": float(best_feasible["metric.full.rolling_60_closed_trades_median"]),
            "frequency_p10": float(best_feasible["metric.full.rolling_60_closed_trades_p10"]),
        },
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "search_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S007 EX14 执行\n\n"
        f"状态：`COMPLETE`。单边10bp固定成本下完成{result.completed_trials}个有效评估，"
        f"结构剪枝{result.pruned_trials}个，失败{result.failed_trials}个；满足全部硬门"
        f"{result.feasible_trials}个，共{evidence['unique_feasible_behaviors']}种独立交易行为。\n",
        encoding="utf-8",
    )
    feasible_summary = (
        "无配置满足全部硬门"
        if best_feasible is None
        else f"最佳合格试验年化{float(best_feasible['objective.cagr']):.2%}、最大回撤"
        f"{float(best_feasible['objective.max_drawdown']):.2%}、卡玛{float(best_feasible['objective.calmar']):.2f}，"
        f"60日闭合交易中位数/P10为"
        f"{float(best_feasible['metric.full.rolling_60_closed_trades_median']):.1f}/"
        f"{float(best_feasible['metric.full.rolling_60_closed_trades_p10']):.1f}"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX14 结论\n\n"
        f"裁决：`{decision}`。{feasible_summary}。全部1200次搜索仅发现{len(feasible)}个合格配置，"
        "该单点只证明搜索空间内存在可行解，尚不能证明存在稳健参数平台。本轮未选配置或创建候选。\n",
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
