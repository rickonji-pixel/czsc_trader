from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX25"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


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


def _passes_hard_gates(
    full: dict[str, float],
    discovery: dict[str, float],
    confirmation: dict[str, float],
    gates: dict[str, float],
) -> bool:
    return bool(
        float(full["cagr"]) >= float(gates["minimum_full_cagr"])
        and float(full["maximum_drawdown"]) >= float(gates["maximum_drawdown_limit"])
        and float(full["maximum_drawdown"]) > float(gates["s001_v2_maximum_drawdown"])
        and float(full["rolling_60_closed_trades_median"])
        >= float(gates["minimum_rolling_60_closed_trades_median"])
        and float(discovery["cagr"]) >= float(gates["minimum_discovery_cagr"])
        and float(confirmation["cagr"]) >= float(gates["minimum_confirmation_cagr"])
        and float(confirmation["maximum_drawdown"])
        >= float(gates["confirmation_maximum_drawdown_limit"])
    )


def _dominance(a: tuple[float, float, float], b: tuple[float, float, float]) -> str:
    tolerance = 1e-12
    b_not_worse = all(right >= left - tolerance for left, right in zip(a, b, strict=True))
    b_strict = any(right > left + tolerance for left, right in zip(a, b, strict=True))
    a_not_worse = all(left >= right - tolerance for left, right in zip(a, b, strict=True))
    a_strict = any(left > right + tolerance for left, right in zip(a, b, strict=True))
    if b_not_worse and b_strict:
        return "B_DOMINATES_A"
    if a_not_worse and a_strict:
        return "A_DOMINATES_B"
    if not b_strict and not a_strict:
        return "EQUAL"
    return "MIXED"


def main() -> None:
    started = time.perf_counter()
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_new_returns"):
        raise ValueError("EX25 protocol identity or return declaration differs")
    if protocol.get("search_started"):
        raise ValueError("EX25 cannot append search trials")
    if any(
        protocol.get(key)
        for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    ):
        raise ValueError("EX25 cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    ex24 = repo / str(sources["ex24_archive"])
    validate_experiment_archive(ex24)
    frozen = {
        ex24 / "experiment_manifest.json": sources["ex24_manifest_sha256"],
        ex24 / "artifacts/protocol.json": sources["ex24_protocol_sha256"],
        ex24 / "artifacts/search_trial_ledger.csv.gz": sources["ex24_search_ledger_sha256"],
        ex24 / "artifacts/parameter_components.csv": sources["ex24_parameter_components_sha256"],
        ex24 / "artifacts/platform_evidence.json": sources["ex24_platform_evidence_sha256"],
        ex24 / "run_experiment.py": sources["ex24_script_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen EX25 source differs: {path}")

    ex24_protocol = _read(ex24 / "artifacts/protocol.json")
    ex24_sources = ex24_protocol["sources"]
    upstream = {
        repo / str(ex24_sources["effective_search_protocol"]): ex24_sources[
            "effective_search_protocol_sha256"
        ],
        repo / str(ex24_sources["feature_panel"]): ex24_sources["feature_panel_sha256"],
        repo / str(ex24_sources["ex09_script"]): ex24_sources["ex09_script_sha256"],
    }
    for path, expected in upstream.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen EX25 upstream source differs: {path}")

    platform_evidence = _read(ex24 / "artifacts/platform_evidence.json")
    components = pd.read_csv(ex24 / "artifacts/parameter_components.csv")
    if components.empty:
        raise ValueError("EX24 parameter components are empty")
    largest = components.sort_values(
        ["unique_behaviors", "parameter_points"], ascending=False
    ).iloc[0]
    trial_ids = json.loads(str(largest["trial_ids"]))
    expected_points = int(platform_evidence["largest_component_parameter_points"])
    if len(trial_ids) != expected_points or len(set(trial_ids)) != expected_points:
        raise ValueError("EX24 largest component trial identities differ")

    ledger = pd.read_csv(ex24 / "artifacts/search_trial_ledger.csv.gz")
    selected = ledger.set_index("trial_id").loc[trial_ids].reset_index()
    if len(selected) != expected_points or not selected["feasible"].astype(bool).all():
        raise ValueError("EX24 largest component is incomplete or contains infeasible trials")

    helpers = _load_helpers(repo / str(ex24_sources["ex09_script"]))
    effective = _read(repo / str(ex24_sources["effective_search_protocol"]))
    panel = pd.read_csv(
        repo / str(ex24_sources["feature_panel"]), parse_dates=["date"]
    ).set_index("date")
    prices = helpers._raw_daily(repo, ex24_sources["daily_sha256"])
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
    segments = ex24_protocol["segments"]
    discovery_mask = scores.index.to_series().between(
        segments["discovery_start"], segments["discovery_end"]
    )
    confirmation_mask = scores.index.to_series().between(
        segments["confirmation_start"], segments["confirmation_end"]
    )
    fee = float(ex24_protocol["execution"]["fee_rate_one_way"])
    gates = ex24_protocol["hard_gates"]
    features = {
        "opp_spx": "risk_global_spx_return",
        "opp_chinext": "risk_chinext_turnover_z20",
        "confirm_share": "micro_share_change_5d_lag1",
        "confirm_volume": "tsfresh__log_volume_change__mean__lb20",
        "entry_vwap": "price_close_vwap_deviation",
        "risk_shibor": "risk_shibor_on_change_5d",
        "risk_range": "price_intraday_range",
    }

    rows: list[dict[str, object]] = []
    for source_row in selected.to_dict(orient="records"):
        params = json.loads(str(source_row["params"]))
        opportunity = float(params["opportunity_total"])
        confirmation_total = float(params["confirmation_total"])
        entry_timing = float(params["entry_timing_total"])
        risk_context = 1.0 - opportunity - confirmation_total - entry_timing
        remaining_total = 1.0 - confirmation_total
        if remaining_total <= 0.0 or risk_context <= 0.0:
            raise ValueError("EX24 base-role weights cannot be renormalized")
        opp_fraction = float(params["opp_spx_fraction"])
        confirm_fraction = float(params["confirm_share_fraction"])
        risk_fraction = float(params["risk_shibor_fraction"])
        base_weights = {
            features["opp_spx"]: opportunity * opp_fraction / remaining_total,
            features["opp_chinext"]: opportunity * (1.0 - opp_fraction) / remaining_total,
            features["entry_vwap"]: entry_timing / remaining_total,
            features["risk_shibor"]: risk_context * risk_fraction / remaining_total,
            features["risk_range"]: risk_context * (1.0 - risk_fraction) / remaining_total,
        }
        if not np.isclose(sum(base_weights.values()), 1.0, atol=1e-12):
            raise ValueError("decoupled base-role weights do not sum to one")
        base_score = scores[list(base_weights)].mul(pd.Series(base_weights), axis=1).sum(axis=1)
        base_score = base_score.where(scores[list(base_weights)].notna().all(axis=1))
        discovery_values = base_score.loc[discovery_mask].dropna()
        entry_threshold = float(discovery_values.quantile(float(params["entry_quantile"])))
        exit_threshold = float(discovery_values.quantile(float(params["exit_quantile"])))
        if exit_threshold >= entry_threshold:
            raise ValueError("decoupled thresholds are invalid")

        base_decision = helpers._hysteresis(base_score, entry_threshold, exit_threshold)
        base_entries = base_decision.gt(base_decision.shift(1, fill_value=0.0)) & discovery_mask
        confirmation_score = (
            scores[features["confirm_share"]] * confirm_fraction
            + scores[features["confirm_volume"]] * (1.0 - confirm_fraction)
        )
        threshold_source = confirmation_score.loc[base_entries].dropna()
        if len(threshold_source) < 20:
            raise ValueError("decoupled confirmation gate has too few discovery entries")
        confirmation_threshold = float(
            threshold_source.quantile(float(params["confirmation_gate_quantile"]))
        )
        decision = _gated_hysteresis(
            base_score,
            confirmation_score,
            entry_threshold,
            exit_threshold,
            confirmation_threshold,
        )
        target = decision.shift(1).fillna(0.0)
        full = helpers._metrics(target, prices, fee)
        discovery = helpers._metrics(target.loc[discovery_mask], prices.loc[discovery_mask], fee)
        confirmation = helpers._metrics(
            target.loc[confirmation_mask], prices.loc[confirmation_mask], fee
        )
        b_feasible = _passes_hard_gates(full, discovery, confirmation, gates)
        b_behavior_hash = hashlib.sha256(target.to_numpy(dtype=np.int8).tobytes()).hexdigest()
        a_metrics = (
            float(source_row["metric.full.cagr"]),
            float(source_row["metric.full.maximum_drawdown"]),
            float(source_row["metric.full.calmar"]),
        )
        b_metrics = (
            float(full["cagr"]),
            float(full["maximum_drawdown"]),
            float(full["calmar"]),
        )
        rows.append({
            "trial_id": source_row["trial_id"],
            "confirmation_total_removed": confirmation_total,
            "behavior_identical": b_behavior_hash == source_row["behavior_hash"],
            "a_behavior_hash": source_row["behavior_hash"],
            "b_behavior_hash": b_behavior_hash,
            "b_feasible": b_feasible,
            "dominance": _dominance(a_metrics, b_metrics),
            "a_cagr": a_metrics[0],
            "b_cagr": b_metrics[0],
            "delta_cagr": b_metrics[0] - a_metrics[0],
            "a_maximum_drawdown": a_metrics[1],
            "b_maximum_drawdown": b_metrics[1],
            "delta_maximum_drawdown": b_metrics[1] - a_metrics[1],
            "a_calmar": a_metrics[2],
            "b_calmar": b_metrics[2],
            "delta_calmar": b_metrics[2] - a_metrics[2],
            "a_frequency_median": float(
                source_row["metric.full.rolling_60_closed_trades_median"]
            ),
            "b_frequency_median": float(full["rolling_60_closed_trades_median"]),
            "delta_frequency_median": float(full["rolling_60_closed_trades_median"])
            - float(source_row["metric.full.rolling_60_closed_trades_median"]),
            "a_frequency_p10": float(source_row["metric.full.rolling_60_closed_trades_p10"]),
            "b_frequency_p10": float(full["rolling_60_closed_trades_p10"]),
            "a_closed_trades": int(source_row["metric.full.closed_trades"]),
            "b_closed_trades": int(full["closed_trades"]),
            "b_discovery_cagr": float(discovery["cagr"]),
            "b_confirmation_cagr": float(confirmation["cagr"]),
            "b_confirmation_maximum_drawdown": float(confirmation["maximum_drawdown"]),
            "b_entry_threshold": entry_threshold,
            "b_exit_threshold": exit_threshold,
            "b_confirmation_threshold": confirmation_threshold,
            "b_base_weights": json.dumps(base_weights, sort_keys=True),
        })

    paired = pd.DataFrame(rows)
    paired.to_csv(
        artifacts / "paired_ablation_results.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    quantile_rows = []
    for metric in (
        "delta_cagr",
        "delta_maximum_drawdown",
        "delta_calmar",
        "delta_frequency_median",
    ):
        values = paired[metric]
        quantile_rows.append({
            "metric": metric,
            "minimum": float(values.min()),
            "p10": float(values.quantile(0.10)),
            "median": float(values.median()),
            "p90": float(values.quantile(0.90)),
            "maximum": float(values.max()),
        })
    pd.DataFrame(quantile_rows).to_csv(
        artifacts / "paired_delta_summary.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )

    count = len(paired)
    identical_share = float(paired["behavior_identical"].mean())
    feasible_retention = float(paired["b_feasible"].mean())
    b_dominance_share = float(paired["dominance"].eq("B_DOMINATES_A").mean())
    a_dominance_share = float(paired["dominance"].eq("A_DOMINATES_B").mean())
    median_deltas = {
        "cagr": float(paired["delta_cagr"].median()),
        "maximum_drawdown": float(paired["delta_maximum_drawdown"].median()),
        "calmar": float(paired["delta_calmar"].median()),
        "frequency_median": float(paired["delta_frequency_median"].median()),
    }
    rule = protocol["decision_rule"]
    equivalent = (
        identical_share >= float(rule["equivalence_identical_behavior_share"])
        and feasible_retention >= float(rule["equivalence_minimum_feasible_retention"])
    )
    improved = (
        feasible_retention >= float(rule["improvement_minimum_feasible_retention"])
        and median_deltas["cagr"] >= 0.0
        and median_deltas["maximum_drawdown"] >= 0.0
        and median_deltas["calmar"] >= 0.0
        and b_dominance_share > a_dominance_share
    )
    retained = feasible_retention < float(rule["retain_maximum_feasible_retention"]) or (
        a_dominance_share >= float(rule["retain_minimum_a_dominance_share"])
        and b_dominance_share < float(rule["retain_maximum_b_dominance_share"])
        and median_deltas["cagr"] <= 0.0
        and median_deltas["maximum_drawdown"] <= 0.0
        and median_deltas["calmar"] <= 0.0
    )
    decision = (
        "REMOVE_CONFIRMATION_FROM_BASE_SCORE"
        if equivalent or improved
        else "RETAIN_CONFIRMATION_IN_BASE_SCORE"
        if retained
        else "PROCEED_TO_DECOUPLED_JOINT_SEARCH_REVIEW"
    )
    elapsed = time.perf_counter() - started
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": decision,
        "runtime_seconds": round(elapsed, 3),
        "paired_parameter_points": count,
        "identical_behavior_points": int(paired["behavior_identical"].sum()),
        "identical_behavior_share": identical_share,
        "decoupled_feasible_points": int(paired["b_feasible"].sum()),
        "decoupled_feasible_retention": feasible_retention,
        "b_dominates_a_points": int(paired["dominance"].eq("B_DOMINATES_A").sum()),
        "b_dominates_a_share": b_dominance_share,
        "a_dominates_b_points": int(paired["dominance"].eq("A_DOMINATES_B").sum()),
        "a_dominates_b_share": a_dominance_share,
        "mixed_points": int(paired["dominance"].eq("MIXED").sum()),
        "equal_points": int(paired["dominance"].eq("EQUAL").sum()),
        "paired_median_deltas": median_deltas,
        "decision_conditions": {
            "equivalence_condition_met": equivalent,
            "improvement_condition_met": improved,
            "retention_condition_met": retained,
        },
        "search_trials_added": 0,
        "configuration_selected": False,
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "ablation_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S007 EX25 执行\n\n"
        f"状态：`COMPLETE`。在{elapsed:.2f}秒内完成{count}个EX24主平台参数点的配对消融；"
        f"B组有{int(paired['b_feasible'].sum())}个保留全部硬门，"
        "没有追加参数搜索。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX25 结论\n\n"
        f"裁决：`{decision}`。移除确认信息的基础分贡献后，交易行为完全相同率"
        f"{identical_share:.2%}，硬门保留率{feasible_retention:.2%}；B组/A组Pareto支配比例"
        f"{b_dominance_share:.2%}/{a_dominance_share:.2%}。年化、最大回撤和卡玛的配对中位变化为"
        f"{median_deltas['cagr']:+.2%}/{median_deltas['maximum_drawdown']:+.2%}/"
        f"{median_deltas['calmar']:+.3f}。未重新优化的B组仍有{int(paired['b_feasible'].sum())}个参数点"
        "满足全部硬门，证据支持对解耦原型做独立联合搜索，不支持直接删除或保留基础分职责。"
        "本轮未选择配置或创建候选，需先与用户评审。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": decision,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
