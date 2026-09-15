from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX16"


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


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_new_returns"):
        raise ValueError("EX16 protocol identity or return declaration differs")
    forbidden = ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX16 cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    ex14 = repo / str(sources["ex14_archive"])
    ex15 = repo / "experiments/S007/20260915_S007_EX15"
    validate_experiment_archive(ex14)
    validate_experiment_archive(ex15)
    frozen = {
        ex14 / "experiment_manifest.json": sources["ex14_manifest_sha256"],
        ex14 / "artifacts/feasible_trials.csv": sources["feasible_trials_sha256"],
        ex14 / "run_experiment.py": sources["ex14_script_sha256"],
        ex15 / "experiment_manifest.json": sources["ex15_manifest_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen neighborhood source differs: {path}")

    ex14_protocol = _read(ex14 / "artifacts/protocol.json")
    ex08 = repo / str(ex14_protocol["sources"]["ex08_archive"])
    effective = _read(ex08 / "artifacts/effective_search_protocol.json")
    ex09_script = repo / "experiments/S007/20260915_S007_EX09/run_experiment.py"
    helpers = _load_helpers(ex09_script)
    panel = pd.read_csv(repo / str(ex14_protocol["sources"]["feature_panel"]), parse_dates=["date"]).set_index("date")
    prices = helpers._raw_daily(repo, ex14_protocol["sources"]["daily_sha256"])
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

    segments = ex14_protocol["segments"]
    discovery_mask = scores.index.to_series().between(segments["discovery_start"], segments["discovery_end"])
    confirmation_mask = scores.index.to_series().between(segments["confirmation_start"], segments["confirmation_end"])
    bounds = effective["search"]["parameters"]
    gates = effective["hard_gates"]
    fee = float(protocol["cost_policy"]["fee_rate_one_way"])
    features = {
        "opp_spx": "risk_global_spx_return",
        "opp_chinext": "risk_chinext_turnover_z20",
        "confirm_share": "micro_share_change_5d_lag1",
        "confirm_volume": "tsfresh__log_volume_change__mean__lb20",
        "entry_vwap": "price_close_vwap_deviation",
        "risk_shibor": "risk_shibor_on_change_5d",
        "risk_range": "price_intraday_range",
    }
    center_frame = pd.read_csv(ex14 / "artifacts/feasible_trials.csv")
    if len(center_frame) != 1:
        raise ValueError("EX16 requires exactly one EX14 feasible center")
    center = json.loads(center_frame.iloc[0]["params"])
    param_bounds = {
        "opportunity_total": tuple(map(float, bounds["opportunity_total"])),
        "confirmation_total": tuple(map(float, bounds["confirmation_total"])),
        "entry_timing_total": tuple(map(float, bounds["entry_timing_total"])),
        "opp_spx_fraction": tuple(map(float, bounds["within_role_fraction"])),
        "confirm_share_fraction": tuple(map(float, bounds["within_role_fraction"])),
        "risk_shibor_fraction": tuple(map(float, bounds["within_role_fraction"])),
        "entry_quantile": tuple(map(float, bounds["entry_quantile"])),
        "exit_quantile": tuple(map(float, bounds["exit_quantile"])),
    }
    audit = protocol["local_audit"]
    rng = np.random.default_rng(int(audit["seed"]))
    radius = float(audit["normalized_radius"])
    samples = [center]
    for _ in range(int(audit["random_perturbations"])):
        sample: dict[str, float] = {}
        for name, (low, high) in param_bounds.items():
            value = float(center[name]) + float(rng.uniform(-radius, radius)) * (high - low)
            sample[name] = float(np.clip(value, low, high))
        samples.append(sample)

    rows: list[dict[str, object]] = []
    for sample_id, params in enumerate(samples):
        opportunity = params["opportunity_total"]
        confirmation = params["confirmation_total"]
        entry_timing = params["entry_timing_total"]
        risk_context = 1.0 - opportunity - confirmation - entry_timing
        low_risk, high_risk = map(float, bounds["risk_context_remainder"])
        if not low_risk <= risk_context <= high_risk:
            rows.append({"sample_id": sample_id, "state": "REJECTED", "rejection": "RISK_WEIGHT_OUT_OF_RANGE", "params": json.dumps(params, sort_keys=True)})
            continue
        if params["exit_quantile"] > params["entry_quantile"] - float(bounds["minimum_quantile_gap"]):
            rows.append({"sample_id": sample_id, "state": "REJECTED", "rejection": "THRESHOLD_GAP_TOO_SMALL", "params": json.dumps(params, sort_keys=True)})
            continue
        weights = {
            features["opp_spx"]: opportunity * params["opp_spx_fraction"],
            features["opp_chinext"]: opportunity * (1.0 - params["opp_spx_fraction"]),
            features["confirm_share"]: confirmation * params["confirm_share_fraction"],
            features["confirm_volume"]: confirmation * (1.0 - params["confirm_share_fraction"]),
            features["entry_vwap"]: entry_timing,
            features["risk_shibor"]: risk_context * params["risk_shibor_fraction"],
            features["risk_range"]: risk_context * (1.0 - params["risk_shibor_fraction"]),
        }
        if max(weights.values()) > float(bounds["maximum_single_factor_weight"]):
            rows.append({"sample_id": sample_id, "state": "REJECTED", "rejection": "FACTOR_WEIGHT_TOO_LARGE", "params": json.dumps(params, sort_keys=True)})
            continue
        combined = sum(scores[name] * weight for name, weight in weights.items()).where(scores.notna().all(axis=1))
        discovery_values = combined.loc[discovery_mask].dropna()
        entry_threshold = float(discovery_values.quantile(params["entry_quantile"]))
        exit_threshold = float(discovery_values.quantile(params["exit_quantile"]))
        decision = helpers._hysteresis(combined, entry_threshold, exit_threshold)
        target = decision.shift(1).fillna(0.0)
        full = helpers._metrics(target, prices, fee)
        discovery = helpers._metrics(target.loc[discovery_mask], prices.loc[discovery_mask], fee)
        confirmation_metrics = helpers._metrics(target.loc[confirmation_mask], prices.loc[confirmation_mask], fee)
        checks = {
            "full_cagr": float(full["cagr"]) >= float(gates["minimum_full_cagr"]),
            "full_drawdown": float(full["maximum_drawdown"]) >= float(gates["maximum_drawdown_limit"]),
            "s001_drawdown": float(full["maximum_drawdown"]) > float(gates["s001_v2_maximum_drawdown"]),
            "frequency_median": float(full["rolling_60_closed_trades_median"]) >= float(gates["minimum_rolling_60_closed_trades_median"]),
            "frequency_p10": float(full["rolling_60_closed_trades_p10"]) >= float(gates["minimum_rolling_60_closed_trades_p10"]),
            "discovery_cagr": float(discovery["cagr"]) >= float(gates["minimum_discovery_cagr"]),
            "confirmation_cagr": float(confirmation_metrics["cagr"]) >= float(gates["minimum_confirmation_cagr"]),
            "confirmation_drawdown": float(confirmation_metrics["maximum_drawdown"]) >= float(gates["confirmation_maximum_drawdown_limit"]),
        }
        rows.append({
            "sample_id": sample_id,
            "state": "COMPLETE",
            "rejection": "",
            "feasible": all(checks.values()),
            "behavior_hash": hashlib.sha256(target.to_numpy(dtype="int8").tobytes()).hexdigest(),
            "cagr": float(full["cagr"]),
            "maximum_drawdown": float(full["maximum_drawdown"]),
            "calmar": float(full["calmar"]),
            "frequency_median": float(full["rolling_60_closed_trades_median"]),
            "frequency_p10": float(full["rolling_60_closed_trades_p10"]),
            "closed_trades": float(full["closed_trades"]),
            **{f"pass_{name}": passed for name, passed in checks.items()},
            "params": json.dumps(params, sort_keys=True),
        })

    frame = pd.DataFrame(rows)
    frame.to_csv(artifacts / "local_perturbations.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    completed = frame.loc[frame["state"].eq("COMPLETE")]
    feasible = completed.loc[completed["feasible"].eq(True)]
    unique_behaviors = int(feasible["behavior_hash"].nunique()) if len(feasible) else 0
    platform_found = (
        unique_behaviors >= int(audit["minimum_unique_behaviors"])
        and unique_behaviors >= int(audit["minimum_connected_unique_behaviors"])
    )
    decision = "PROCEED_TO_CONFIGURATION_REVIEW" if platform_found else "STOP_LOCAL_10BP_PLATFORM_NOT_FOUND"
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": decision,
        "requested_perturbations": int(audit["random_perturbations"]),
        "completed_perturbations_including_center": int(len(completed)),
        "rejected_perturbations": int(frame["state"].eq("REJECTED").sum()),
        "feasible_perturbations_including_center": int(len(feasible)),
        "feasible_rate": float(len(feasible) / max(len(completed), 1)),
        "unique_feasible_behaviors": unique_behaviors,
        "platform_found": bool(platform_found),
        "minimum_unique_behaviors": int(audit["minimum_unique_behaviors"]),
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "neighborhood_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S007 EX16 执行\n\n"
        f"状态：`COMPLETE`。中心点外生成{int(audit['random_perturbations'])}个局部扰动；"
        f"{len(completed)}个含中心样本完成绩效计算，{int(frame['state'].eq('REJECTED').sum())}个因结构约束拒绝。"
        f"合格{len(feasible)}个，形成{unique_behaviors}种交易行为。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX16 结论\n\n"
        f"裁决：`{decision}`。局部合格率{float(evidence['feasible_rate']):.2%}，"
        f"合格交易行为{unique_behaviors}种；参数平台判定为`{'PASS' if platform_found else 'FAIL'}`。"
        "本轮只审计唯一合格点的邻域稳定性，不创建候选。\n",
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
