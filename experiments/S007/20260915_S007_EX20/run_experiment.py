from __future__ import annotations

import hashlib
import importlib.util
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
    run_search,
)


EXPERIMENT_ID = "20260915_S007_EX20"


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


def _gated_hysteresis(
    base_score: pd.Series,
    confirmation_score: pd.Series,
    entry_threshold: float,
    exit_threshold: float,
    confirmation_threshold: float,
) -> pd.Series:
    current = 0.0
    output = np.zeros(len(base_score), dtype=float)
    for index, (base, confirmation) in enumerate(
        zip(base_score.to_numpy(dtype=float), confirmation_score.to_numpy(dtype=float), strict=True)
    ):
        if np.isfinite(base):
            if current == 0.0 and base >= entry_threshold and confirmation >= confirmation_threshold:
                current = 1.0
            elif current == 1.0 and base <= exit_threshold:
                current = 0.0
        output[index] = current
    return pd.Series(output, index=base_score.index, name="decision_target")


def _trade_diagnostics(
    execution_target: pd.Series,
    prices: pd.DataFrame,
    fee: float,
    short_holding_max_sessions: int,
) -> dict[str, float | int]:
    target = execution_target.reindex(prices.index).fillna(0.0).astype(float)
    prior = target.shift(1, fill_value=0.0)
    entries = list(target.index[target.gt(prior)])
    exits = list(target.index[target.lt(prior)])
    rows: list[dict[str, float | int]] = []
    exit_cursor = 0
    open_trades = 0
    for entry in entries:
        while exit_cursor < len(exits) and exits[exit_cursor] <= entry:
            exit_cursor += 1
        if exit_cursor >= len(exits):
            open_trades += 1
            continue
        exit_ = exits[exit_cursor]
        exit_cursor += 1
        holding = int(prices.index.get_loc(exit_) - prices.index.get_loc(entry))
        net_return = float(
            prices.loc[exit_, "open"] * (1.0 - fee)
            / (prices.loc[entry, "open"] * (1.0 + fee))
            - 1.0
        )
        rows.append({"holding_sessions": holding, "net_return": net_return})
    frame = pd.DataFrame(rows)
    if frame.empty:
        return {
            "closed_trades_from_ledger": 0,
            "open_trades": open_trades,
            "short_holding_trade_share": 0.0,
            "short_holding_average_net_return": 0.0,
            "short_holding_loss_share": 0.0,
        }
    short = frame["holding_sessions"].le(short_holding_max_sessions)
    losses = frame["net_return"].clip(upper=0.0).abs()
    total_loss = float(losses.sum())
    return {
        "closed_trades_from_ledger": int(len(frame)),
        "open_trades": int(open_trades),
        "short_holding_trade_share": float(short.mean()),
        "short_holding_average_net_return": float(frame.loc[short, "net_return"].mean()) if short.any() else 0.0,
        "short_holding_loss_share": float(losses.loc[short].sum() / total_loss) if total_loss > 0 else 0.0,
    }


def _platforms(frame: pd.DataFrame, step: float, minimum_points: int, minimum_span: float) -> list[dict[str, object]]:
    feasible = frame.loc[frame["feasible"]].sort_values("confirmation_gate_quantile")
    groups: list[list[pd.Series]] = []
    current: list[pd.Series] = []
    for _, row in feasible.iterrows():
        if current and not np.isclose(float(row["confirmation_gate_quantile"]) - float(current[-1]["confirmation_gate_quantile"]), step):
            groups.append(current)
            current = []
        current.append(row)
    if current:
        groups.append(current)
    output = []
    for index, group in enumerate(groups, start=1):
        low = float(group[0]["confirmation_gate_quantile"])
        high = float(group[-1]["confirmation_gate_quantile"])
        if len(group) >= minimum_points and high - low + 1e-12 >= minimum_span:
            subset = pd.DataFrame(group)
            output.append({
                "platform_id": f"PLATFORM-{index:02d}",
                "minimum_quantile": low,
                "maximum_quantile": high,
                "quantile_span": high - low,
                "grid_points": len(group),
                "unique_behaviors": int(subset["behavior_hash"].nunique()),
                "minimum_cagr": float(subset["full_cagr"].min()),
                "maximum_cagr": float(subset["full_cagr"].max()),
                "worst_maximum_drawdown": float(subset["full_maximum_drawdown"].min()),
                "minimum_frequency_median": float(subset["rolling_60_closed_trades_median"].min()),
                "minimum_frequency_p10": float(subset["rolling_60_closed_trades_p10"].min()),
            })
    return output


def main() -> None:
    started = time.perf_counter()
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_new_returns"):
        raise ValueError("EX20 protocol identity or return declaration differs")
    if not protocol.get("search_started"):
        raise ValueError("EX20 must execute the preregistered search")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX20 cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    ex19 = repo / str(sources["ex19_archive"])
    validate_experiment_archive(ex19)
    frozen = {
        ex19 / "experiment_manifest.json": sources["ex19_manifest_sha256"],
        ex19 / "artifacts/protocol.json": sources["ex19_protocol_sha256"],
        ex19 / "artifacts/component_contract.json": sources["component_contract_sha256"],
        ex19 / "artifacts/search_protocol.json": sources["search_protocol_sha256"],
        repo / str(sources["effective_search_protocol"]): sources["effective_search_protocol_sha256"],
        repo / str(sources["feature_panel"]): sources["feature_panel_sha256"],
        repo / str(sources["ex09_script"]): sources["ex09_script_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen EX20 source differs: {path}")

    component = _read(ex19 / "artifacts/component_contract.json")
    search_protocol = _read(ex19 / "artifacts/search_protocol.json")
    effective = _read(repo / str(sources["effective_search_protocol"]))
    helpers = _load_helpers(repo / str(sources["ex09_script"]))
    panel = pd.read_csv(repo / str(sources["feature_panel"]), parse_dates=["date"]).set_index("date")
    prices = helpers._raw_daily(repo, sources["daily_sha256"])
    prices = prices.loc[prices.index <= pd.Timestamp(protocol["development_cutoff"])]
    if not panel.index.equals(prices.index):
        raise ValueError("feature panel and price calendar differ")

    normalization = component["normalization"]
    scores = pd.DataFrame(index=panel.index)
    for feature, binding in effective["feature_bindings"].items():
        scores[feature] = helpers._causal_percentile(
            panel[feature],
            int(normalization["lookback_sessions"]),
            int(normalization["minimum_observations"]),
        ) * int(binding["orientation"])
    weights = {name: float(value) for name, value in component["base_score"]["weights"].items()}
    if set(weights) != set(scores):
        raise ValueError("EX19 weights and effective score features differ")
    contributions = scores.mul(pd.Series(weights), axis=1)
    base_score = contributions.sum(axis=1).where(scores.notna().all(axis=1))
    confirmation_names = list(component["confirmation_score"]["weights"])
    confirmation_score = contributions[confirmation_names].sum(axis=1)
    entry_threshold = float(component["base_score"]["entry_threshold"])
    exit_threshold = float(component["base_score"]["exit_threshold"])
    segments = protocol["segments"]
    discovery_mask = base_score.index.to_series().between(segments["discovery_start"], segments["discovery_end"])
    confirmation_mask = base_score.index.to_series().between(segments["confirmation_start"], segments["confirmation_end"])

    original_decision = helpers._hysteresis(base_score, entry_threshold, exit_threshold)
    original_entries = original_decision.gt(original_decision.shift(1, fill_value=0.0)) & discovery_mask
    threshold_source = confirmation_score.loc[original_entries].dropna()
    if len(threshold_source) < 20:
        raise ValueError("too few discovery entries to resolve confirmation quantiles")

    quantiles = [float(value) for value in search_protocol["parameters"]["confirmation_gate_quantile"]["values"]]
    fixed = search_protocol["fixed_parameters"]
    gates = search_protocol["hard_gates"]
    fee = float(fixed["fee_rate_one_way"])
    short_holding_max = int(protocol["short_holding_max_sessions"])
    original_target = original_decision.shift(1).fillna(0.0)
    original_metrics = helpers._metrics(original_target, prices, fee)
    original_metrics_0bp = helpers._metrics(original_target, prices, 0.0)
    original_trade = _trade_diagnostics(original_target, prices, fee, short_holding_max)

    def evaluator(params: dict[str, object]) -> SearchEvaluation:
        quantile = float(params["confirmation_gate_quantile"])
        confirmation_threshold = float(threshold_source.quantile(quantile))
        decision = _gated_hysteresis(
            base_score,
            confirmation_score,
            entry_threshold,
            exit_threshold,
            confirmation_threshold,
        )
        target = decision.shift(1).fillna(0.0)
        full = helpers._metrics(target, prices, fee)
        full_0bp = helpers._metrics(target, prices, 0.0)
        discovery = helpers._metrics(target.loc[discovery_mask], prices.loc[discovery_mask], fee)
        confirmation = helpers._metrics(target.loc[confirmation_mask], prices.loc[confirmation_mask], fee)
        trade = _trade_diagnostics(target, prices, fee, short_holding_max)
        payload = {
            "base_anchor_trial_id": component["base_anchor_trial_id"],
            "confirmation_gate_quantile": quantile,
            "confirmation_threshold": confirmation_threshold,
        }
        strategy_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        behavior_hash = hashlib.sha256(target.to_numpy(dtype=np.int8).tobytes()).hexdigest()
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
                "confirmation_cagr_shortfall": float(gates["minimum_confirmation_cagr"]) - float(confirmation["cagr"]),
                "confirmation_drawdown_loss": abs(min(float(confirmation["maximum_drawdown"]), 0.0)),
            },
            strategy_hash=strategy_hash,
            behavior_hash=behavior_hash,
            metadata={
                "confirmation_threshold": confirmation_threshold,
                "full": full,
                "full_0bp": full_0bp,
                "discovery": discovery,
                "confirmation": confirmation,
                "trade": trade,
                "cost_drag_0bp_to_10bp_cagr": float(full_0bp["cagr"] - full["cagr"]),
            },
        )

    spec = SearchSpec(
        study_name="S007-EX20-CONFIRMATION-GATE",
        method="grid",
        parameters=(FloatParameter("confirmation_gate_quantile", 0.0, 0.5, step=0.025),),
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
        target_trials=len(quantiles),
        seed=int(search_protocol["seed"]),
        storage_path=None,
        grid={"confirmation_gate_quantile": quantiles},
        workers=int(search_protocol["workers"]),
    )
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    result = run_search(spec, evaluator)
    ledger = result.ledger.copy()
    metadata = pd.json_normalize(ledger["metadata"].map(json.loads)).add_prefix("metric.")
    enriched = pd.concat([ledger, metadata], axis=1)
    rows = pd.DataFrame({
        "trial_id": enriched["trial_id"],
        "confirmation_gate_quantile": enriched["params"].map(lambda value: float(json.loads(value)["confirmation_gate_quantile"])),
        "confirmation_threshold": enriched["metric.confirmation_threshold"],
        "feasible": enriched["feasible"].fillna(False).astype(bool),
        "behavior_hash": enriched["behavior_hash"],
        "full_cagr": enriched["metric.full.cagr"],
        "full_total_return": enriched["metric.full.total_return"],
        "full_maximum_drawdown": enriched["metric.full.maximum_drawdown"],
        "full_calmar": enriched["metric.full.calmar"],
        "closed_trades": enriched["metric.full.closed_trades"],
        "rolling_60_closed_trades_median": enriched["metric.full.rolling_60_closed_trades_median"],
        "rolling_60_closed_trades_p10": enriched["metric.full.rolling_60_closed_trades_p10"],
        "exposure_ratio": enriched["metric.full.exposure_ratio"],
        "short_holding_trade_share": enriched["metric.trade.short_holding_trade_share"],
        "short_holding_average_net_return": enriched["metric.trade.short_holding_average_net_return"],
        "short_holding_loss_share": enriched["metric.trade.short_holding_loss_share"],
        "cost_drag_0bp_to_10bp_cagr": enriched["metric.cost_drag_0bp_to_10bp_cagr"],
        "discovery_cagr": enriched["metric.discovery.cagr"],
        "discovery_maximum_drawdown": enriched["metric.discovery.maximum_drawdown"],
        "confirmation_cagr": enriched["metric.confirmation.cagr"],
        "confirmation_maximum_drawdown": enriched["metric.confirmation.maximum_drawdown"],
    }).sort_values("confirmation_gate_quantile").reset_index(drop=True)
    if len(rows) != len(quantiles) or rows["confirmation_gate_quantile"].nunique() != len(quantiles):
        raise ValueError("EX20 did not evaluate every preregistered grid point exactly once")
    if not np.allclose(rows["confirmation_gate_quantile"].to_numpy(), np.array(quantiles)):
        raise ValueError("EX20 grid ledger differs from preregistered order")

    platform_rule = search_protocol["platform"]
    platforms = _platforms(
        rows,
        0.025,
        int(platform_rule["minimum_consecutive_feasible_grid_points"]),
        float(platform_rule["minimum_quantile_span"]),
    )
    failure_counts = {
        "full_cagr": int(rows["full_cagr"].lt(float(gates["minimum_full_cagr"])).sum()),
        "full_maximum_drawdown": int(rows["full_maximum_drawdown"].lt(float(gates["maximum_drawdown_limit"])).sum()),
        "frequency_median": int(rows["rolling_60_closed_trades_median"].lt(float(gates["minimum_rolling_60_closed_trades_median"])).sum()),
        "frequency_p10": int(rows["rolling_60_closed_trades_p10"].lt(float(gates["minimum_rolling_60_closed_trades_p10"])).sum()),
        "discovery_cagr": int(rows["discovery_cagr"].lt(float(gates["minimum_discovery_cagr"])).sum()),
        "confirmation_cagr": int(rows["confirmation_cagr"].lt(float(gates["minimum_confirmation_cagr"])).sum()),
        "confirmation_maximum_drawdown": int(rows["confirmation_maximum_drawdown"].lt(float(gates["confirmation_maximum_drawdown_limit"])).sum()),
    }
    decision = "REVIEW_GATED_SCORE_PLATFORM_FOUND" if platforms else "STOP_GATED_SCORE_NO_PLATFORM"
    rows.to_csv(artifacts / "grid_results.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    enriched.to_csv(artifacts / "search_trial_ledger.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    _write(artifacts / "platforms.json", {"schema_version": 1, "platforms": platforms})

    best = rows.sort_values(["feasible", "full_calmar", "full_cagr"], ascending=[False, False, False]).iloc[0]
    q0 = rows.loc[rows["confirmation_gate_quantile"].eq(0.0)].iloc[0]
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": decision,
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "grid_points": len(rows),
        "feasible_grid_points": int(rows["feasible"].sum()),
        "unique_behaviors": int(rows["behavior_hash"].nunique()),
        "discovery_entry_threshold_source_count": int(len(threshold_source)),
        "platform_count": len(platforms),
        "platforms": platforms,
        "failure_counts": failure_counts,
        "base_anchor_10bp": {
            **original_metrics,
            **original_trade,
            "cost_drag_0bp_to_10bp_cagr": float(original_metrics_0bp["cagr"] - original_metrics["cagr"]),
        },
        "q0_grid_point": q0.to_dict(),
        "best_completed": best.to_dict(),
        "best_feasible": None,
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "evaluation_evidence.json", evidence)

    if platforms:
        platform_text = "; ".join(
            f"Q{item['minimum_quantile']:.3f}—Q{item['maximum_quantile']:.3f}（{item['grid_points']}点）"
            for item in platforms
        )
    else:
        platform_text = "无"
    (experiment / "03_execution.md").write_text(
        "# S007 EX20 执行\n\n"
        f"状态：`COMPLETE`。Optuna内存网格完成{len(rows)}个预注册点，合格{int(rows['feasible'].sum())}点，"
        f"形成{len(platforms)}个平台；发现期用于计算确认阈值的原始入场信号{len(threshold_source)}个。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX20 结论\n\n"
        f"裁决：`{decision}`。合格参数平台：{platform_text}。"
        f"原始10bp锚点年化{float(original_metrics['cagr']):.2%}、最大回撤"
        f"{float(original_metrics['maximum_drawdown']):.2%}、频率"
        f"{float(original_metrics['rolling_60_closed_trades_median']):.1f}/"
        f"{float(original_metrics['rolling_60_closed_trades_p10']):.1f}。Q0是按发现期最小确认贡献"
        f"建立的最低门槛，仍会过滤确认期低于该阈值的入场，因此不是原始锚点的完全复制。"
        f"本轮按卡玛排序的最佳已完成点Q{float(best['confirmation_gate_quantile']):.3f}的年化"
        f"{float(best['full_cagr']):.2%}、最大回撤{float(best['full_maximum_drawdown']):.2%}、"
        f"卡玛{float(best['full_calmar']):.2f}、频率{float(best['rolling_60_closed_trades_median']):.1f}/"
        f"{float(best['rolling_60_closed_trades_p10']):.1f}。{failure_counts['full_maximum_drawdown']}点违反最大回撤门，"
        f"{failure_counts['frequency_median']}点违反频率"
        "中位数门，未形成合格点或平台。本轮未创建候选，需先与用户评审。\n",
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
