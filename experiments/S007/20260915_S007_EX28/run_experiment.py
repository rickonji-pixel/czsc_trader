from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX28"
PERFORMANCE_COLUMNS = ["objective.cagr", "objective.max_drawdown", "objective.calmar"]


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or protocol.get("reads_new_returns"):
        raise ValueError("EX28 protocol identity or return declaration differs")
    forbidden = ("appends_search_trials", "candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX28 cannot search, create, promote, or deploy a candidate")

    sources = protocol["sources"]
    ex27 = repo / str(sources["ex27_archive"])
    validate_experiment_archive(ex27)
    frozen = {
        ex27 / "experiment_manifest.json": sources["ex27_manifest_sha256"],
        ex27 / "artifacts/protocol.json": sources["ex27_protocol_sha256"],
        ex27 / "artifacts/search_trial_ledger.csv.gz": sources["ex27_search_ledger_sha256"],
        ex27 / "artifacts/parameter_components.csv": sources["ex27_parameter_components_sha256"],
        ex27 / "artifacts/local_neighborhood_summary.csv": sources["ex27_local_neighborhood_sha256"],
        ex27 / "artifacts/platform_evidence.json": sources["ex27_platform_evidence_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen EX28 source differs: {path}")

    components = pd.read_csv(ex27 / "artifacts/parameter_components.csv").sort_values(
        ["unique_behaviors", "parameter_points"], ascending=False
    )
    source_component = components.iloc[0]
    trial_ids = json.loads(str(source_component["trial_ids"]))
    ledger = pd.read_csv(ex27 / "artifacts/search_trial_ledger.csv.gz").set_index("trial_id")
    platform = ledger.loc[trial_ids].copy()
    neighborhoods = pd.read_csv(ex27 / "artifacts/local_neighborhood_summary.csv").set_index("trial_id")
    platform = platform.join(neighborhoods[["local_feasible_rate", "neighborhood_radius"]], how="left")
    if platform[["local_feasible_rate", "neighborhood_radius"]].isna().any().any():
        raise ValueError("EX27 platform lacks neighborhood evidence")

    parameter_names = list(protocol["effective_parameter_bounds"])
    parameters = pd.DataFrame(platform["params"].map(json.loads).tolist(), index=platform.index)
    retained_upper = float(protocol["selection"]["retained_entry_quantile_upper"])
    universe_mask = parameters["entry_quantile"].le(retained_upper)
    universe = platform.loc[universe_mask].copy()
    universe_params = parameters.loc[universe_mask].copy()
    if universe.empty:
        raise ValueError("EX28 selection universe is empty")

    bounds = protocol["effective_parameter_bounds"]
    normalized = pd.DataFrame(index=universe.index)
    for name in parameter_names:
        low, high = map(float, bounds[name])
        normalized[name] = (universe_params[name].astype(float) - low) / (high - low)
    values = normalized.to_numpy(dtype=float)
    if not np.isfinite(values).all() or not normalized.ge(-1e-12).all().all() or not normalized.le(1.0 + 1e-12).all().all():
        raise ValueError("EX28 parameters are outside retained bounds")

    parameter_center = normalized.median()
    universe["selection.parameter_center_linf_distance"] = normalized.sub(parameter_center).abs().max(axis=1)
    for column in PERFORMANCE_COLUMNS:
        universe[f"selection.percentile.{column}"] = universe[column].rank(method="average", pct=True)
    universe["selection.minimum_performance_percentile"] = universe[
        [f"selection.percentile.{column}" for column in PERFORMANCE_COLUMNS]
    ].min(axis=1)

    medians = {
        "cagr": float(universe["objective.cagr"].median()),
        "maximum_drawdown": float(universe["objective.max_drawdown"].median()),
        "calmar": float(universe["objective.calmar"].median()),
        "local_feasible_rate": float(universe["local_feasible_rate"].median()),
    }
    eligible_mask = (
        universe["objective.cagr"].ge(medians["cagr"])
        & universe["objective.max_drawdown"].ge(medians["maximum_drawdown"])
        & universe["objective.calmar"].ge(medians["calmar"])
        & universe["local_feasible_rate"].ge(medians["local_feasible_rate"])
    )
    eligible = universe.loc[eligible_mask].sort_values(
        [
            "selection.parameter_center_linf_distance",
            "local_feasible_rate",
            "selection.minimum_performance_percentile",
            "trial_id",
        ],
        ascending=[True, False, False, True],
    )
    eligible.to_csv(artifacts / "eligible_points.csv", index=True, encoding="utf-8-sig", lineterminator="\n")

    selected = eligible.iloc[0] if len(eligible) else None
    decision = "REVIEW_REPRESENTATIVE_SELECTED" if selected is not None else "REVIEW_NO_REPRESENTATIVE"
    evidence: dict[str, object] = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": decision,
        "source_component_id": str(source_component["component_id"]),
        "source_platform_parameter_points": int(len(platform)),
        "selection_universe_parameter_points": int(len(universe)),
        "excluded_above_retained_entry_upper": int((~universe_mask).sum()),
        "selection_universe_medians": medians,
        "normalized_parameter_center": {name: float(parameter_center[name]) for name in parameter_names},
        "eligible_parameter_points": int(len(eligible)),
        "fallback_relaxed": False,
        "configuration_selected": selected is not None,
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    selected_config: dict[str, object] | None = None
    if selected is not None:
        selected_id = str(selected.name)
        selected_metadata = json.loads(str(selected["metadata"]))
        selected_config = {
            "schema_version": 1,
            "experiment_id": EXPERIMENT_ID,
            "selection_status": "SELECTED_FOR_CANDIDATE_REGISTRATION_REVIEW",
            "proposed_candidate_id": protocol["proposed_candidate_id"],
            "source_trial_id": selected_id,
            "strategy_hash": str(selected["strategy_hash"]),
            "behavior_hash": str(selected["behavior_hash"]),
            "params": json.loads(str(selected["params"])),
            "weights": selected_metadata["weights"],
            "role_totals": selected_metadata["role_totals"],
            "thresholds": {
                "entry": float(selected_metadata["entry_threshold"]),
                "exit": float(selected_metadata["exit_threshold"]),
                "confirmation": float(selected_metadata["confirmation_threshold"]),
            },
            "metrics": {
                "cagr": float(selected["objective.cagr"]),
                "maximum_drawdown": float(selected["objective.max_drawdown"]),
                "calmar": float(selected["objective.calmar"]),
                "closed_trades": int(selected["metric.full.closed_trades"]),
                "rolling_60_closed_trades_median": float(selected["metric.full.rolling_60_closed_trades_median"]),
                "rolling_60_closed_trades_p10_observation": float(selected["metric.full.rolling_60_closed_trades_p10"]),
                "discovery_cagr": float(selected["metric.discovery.cagr"]),
                "confirmation_cagr": float(selected["metric.confirmation.cagr"]),
            },
            "selection_evidence": {
                "parameter_center_linf_distance": float(selected["selection.parameter_center_linf_distance"]),
                "local_feasible_rate": float(selected["local_feasible_rate"]),
                "neighborhood_radius": float(selected["neighborhood_radius"]),
                "minimum_performance_percentile": float(selected["selection.minimum_performance_percentile"]),
            },
            "candidate_created": False,
        }
        evidence["selected_configuration"] = selected_config
        _write(artifacts / "selected_configuration.json", selected_config)

    _write(artifacts / "selection_evidence.json", evidence)
    execution_text = (
        f"从EX27最大平台{len(platform)}个参数点中，按0.80最终入场上界保留{len(universe)}个；"
        f"{len(eligible)}个点同时达到三项绩效与局部可行率中位数门槛。"
    )
    (experiment / "03_execution.md").write_text(
        f"# S007 EX28 执行\n\n状态：`COMPLETE`。{execution_text}\n",
        encoding="utf-8",
    )
    conclusion = (
        "未找到符合预注册规则的平台代表点；未放宽规则。"
        if selected_config is None
        else f"选中试验`{selected_config['source_trial_id']}`作为S007-C001登记前的唯一代表配置。"
        f"年化{selected_config['metrics']['cagr']:.2%}、最大回撤{selected_config['metrics']['maximum_drawdown']:.2%}、"
        f"卡玛{selected_config['metrics']['calmar']:.2f}、滚动60日闭合交易中位数/P10为"
        f"{selected_config['metrics']['rolling_60_closed_trades_median']:.1f}/"
        f"{selected_config['metrics']['rolling_60_closed_trades_p10_observation']:.1f}。"
        "本轮只完成配置选型，尚未创建候选或执行冻结前体检。"
    )
    (experiment / "04_conclusion.md").write_text(
        f"# S007 EX28 结论\n\n裁决：`{decision}`。{conclusion}\n",
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
