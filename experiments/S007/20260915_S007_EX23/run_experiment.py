from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX23"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _components(adjacency: np.ndarray) -> list[list[int]]:
    unseen = set(range(len(adjacency)))
    groups: list[list[int]] = []
    while unseen:
        start = min(unseen)
        stack = [start]
        group: list[int] = []
        unseen.remove(start)
        while stack:
            current = stack.pop()
            group.append(current)
            neighbors = set(np.flatnonzero(adjacency[current])).intersection(unseen)
            unseen.difference_update(neighbors)
            stack.extend(sorted(neighbors, reverse=True))
        groups.append(sorted(group))
    return groups


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or protocol.get("reads_new_returns"):
        raise ValueError("EX23 protocol identity or return-access declaration differs")
    if protocol.get("search_started"):
        raise ValueError("EX23 cannot append search trials")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX23 cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    ex22 = repo / str(sources["ex22_archive"])
    validate_experiment_archive(ex22)
    frozen = {
        ex22 / "experiment_manifest.json": sources["ex22_manifest_sha256"],
        ex22 / "artifacts/search_trial_ledger.csv.gz": sources["search_ledger_sha256"],
        ex22 / "artifacts/feasible_trials.csv": sources["feasible_trials_sha256"],
        ex22 / "artifacts/protocol.json": sources["ex22_protocol_sha256"],
        ex22 / "run_experiment.py": sources["ex22_script_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen EX23 source differs: {path}")

    ex22_protocol = _read(ex22 / "artifacts/protocol.json")
    search = ex22_protocol["search"]
    bounds = search["parameters"]
    bound_map = {
        "opportunity_total": bounds["opportunity_total"],
        "confirmation_total": bounds["confirmation_total"],
        "entry_timing_total": bounds["entry_timing_total"],
        "opp_spx_fraction": bounds["within_role_fraction"],
        "confirm_share_fraction": bounds["within_role_fraction"],
        "risk_shibor_fraction": bounds["within_role_fraction"],
        "entry_quantile": bounds["entry_quantile"],
        "exit_quantile": bounds["exit_quantile"],
        "confirmation_gate_quantile": bounds["confirmation_gate_quantile"],
    }
    parameter_names = list(bound_map)

    ledger = pd.read_csv(ex22 / "artifacts/search_trial_ledger.csv.gz")
    completed = ledger.loc[ledger["state"].eq("COMPLETE")].copy()
    feasible = completed.loc[completed["feasible"].eq(True)].copy()
    if completed.empty or feasible.empty:
        raise ValueError("EX22 completed or feasible ledger is empty")
    completed["parameter_hash"] = completed["params"].map(
        lambda value: hashlib.sha256(json.dumps(json.loads(value), sort_keys=True).encode()).hexdigest()
    )
    feasible["parameter_hash"] = feasible["params"].map(
        lambda value: hashlib.sha256(json.dumps(json.loads(value), sort_keys=True).encode()).hexdigest()
    )
    completed_unique = completed.sort_values(["objective.calmar", "objective.cagr"], ascending=False).drop_duplicates("parameter_hash").reset_index(drop=True)
    feasible_unique = feasible.sort_values(["objective.calmar", "objective.cagr"], ascending=False).drop_duplicates("parameter_hash").reset_index(drop=True)

    def normalized_params(frame: pd.DataFrame) -> pd.DataFrame:
        params = pd.DataFrame(frame["params"].map(json.loads).tolist(), index=frame.index)
        normalized = pd.DataFrame(index=frame.index)
        for name, (low, high) in bound_map.items():
            normalized[name] = (params[name].astype(float) - float(low)) / (float(high) - float(low))
        if not np.isfinite(normalized.to_numpy()).all() or not normalized.ge(-1e-12).all().all() or not normalized.le(1.0 + 1e-12).all().all():
            raise ValueError("normalized parameters are non-finite or outside frozen bounds")
        return normalized

    feasible_normalized = normalized_params(feasible_unique)
    feasible_values = feasible_normalized.to_numpy(dtype=float)
    radius = float(protocol["platform"]["normalized_linf_radius"])
    feasible_distances = np.max(np.abs(feasible_values[:, None, :] - feasible_values[None, :, :]), axis=2)
    groups = _components(feasible_distances <= radius)

    boundary = protocol["boundary_diagnostic"]
    margin = float(boundary["normalized_boundary_margin"])
    dependency_share = float(boundary["dependency_share"])
    component_rows: list[dict[str, object]] = []
    group_details: list[tuple[list[int], int]] = []
    for ordinal, indices in enumerate(groups, start=1):
        group = feasible_unique.iloc[indices]
        normalized = feasible_normalized.iloc[indices]
        unique_behaviors = int(group["behavior_hash"].nunique())
        group_details.append((indices, unique_behaviors))
        parameter_ranges = {
            name: {"minimum": float(normalized[name].min()), "maximum": float(normalized[name].max())}
            for name in parameter_names
        }
        component_rows.append({
            "component_id": f"PARAM-COMPONENT-{ordinal:02d}",
            "parameter_points": int(len(group)),
            "unique_behaviors": unique_behaviors,
            "cagr_min": float(group["objective.cagr"].min()),
            "cagr_median": float(group["objective.cagr"].median()),
            "cagr_max": float(group["objective.cagr"].max()),
            "maximum_drawdown_worst": float(group["objective.max_drawdown"].min()),
            "maximum_drawdown_median": float(group["objective.max_drawdown"].median()),
            "calmar_median": float(group["objective.calmar"].median()),
            "frequency_median_min": float(group["metric.full.rolling_60_closed_trades_median"].min()),
            "parameter_ranges_normalized": json.dumps(parameter_ranges, sort_keys=True),
            "trial_ids": json.dumps(group["trial_id"].tolist()),
        })
    components = pd.DataFrame(component_rows).sort_values(
        ["unique_behaviors", "parameter_points"], ascending=False
    ).reset_index(drop=True)
    components.to_csv(artifacts / "parameter_components.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    largest_indices, largest_behaviors = max(group_details, key=lambda item: (item[1], len(item[0])))
    largest = feasible_unique.iloc[largest_indices].copy()
    largest_normalized = feasible_normalized.iloc[largest_indices]
    boundary_rows = []
    boundary_dependencies = []
    for name in parameter_names:
        values = largest_normalized[name]
        lower_share = float(values.le(margin).mean())
        upper_share = float(values.ge(1.0 - margin).mean())
        side = "LOWER" if lower_share >= dependency_share else "UPPER" if upper_share >= dependency_share else "NONE"
        if side != "NONE":
            boundary_dependencies.append({"parameter": name, "side": side})
        boundary_rows.append({
            "parameter": name,
            "normalized_minimum": float(values.min()),
            "normalized_q10": float(values.quantile(0.10)),
            "normalized_median": float(values.median()),
            "normalized_q90": float(values.quantile(0.90)),
            "normalized_maximum": float(values.max()),
            "lower_boundary_share": lower_share,
            "upper_boundary_share": upper_share,
            "dependency_side": side,
        })
    boundary_summary = pd.DataFrame(boundary_rows)
    boundary_summary.to_csv(artifacts / "parameter_boundary_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    completed_normalized = normalized_params(completed_unique)
    completed_values = completed_normalized.to_numpy(dtype=float)
    completed_feasible = completed_unique["feasible"].astype(bool).to_numpy()
    k = int(protocol["neighborhood_diagnostic"]["nearest_parameter_points"])
    neighborhood_rows = []
    for index, values in enumerate(feasible_values):
        distances = np.max(np.abs(completed_values - values), axis=1)
        nearest = np.argsort(distances, kind="stable")[: min(k, len(distances))]
        neighborhood_rows.append({
            "trial_id": feasible_unique.iloc[index]["trial_id"],
            "behavior_hash": feasible_unique.iloc[index]["behavior_hash"],
            "nearest_points": int(len(nearest)),
            "neighborhood_radius": float(distances[nearest[-1]]),
            "local_feasible_rate": float(completed_feasible[nearest].mean()),
            "local_feasible_points": int(completed_feasible[nearest].sum()),
        })
    neighborhoods = pd.DataFrame(neighborhood_rows)
    neighborhoods.to_csv(artifacts / "local_neighborhood_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    platform = protocol["platform"]
    total_behaviors = int(feasible["behavior_hash"].nunique())
    platform_found = (
        total_behaviors >= int(platform["minimum_unique_behaviors"])
        and largest_behaviors >= int(platform["minimum_connected_unique_behaviors"])
    )
    boundary_dependent = bool(boundary_dependencies)
    decision = (
        "STOP_NO_STABLE_JOINT_PARAMETER_PLATFORM"
        if not platform_found
        else "REVIEW_BOUNDARY_EXTENSION_REQUIRED"
        if boundary_dependent
        else "PROCEED_TO_CONFIGURATION_REVIEW"
    )
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": decision,
        "completed_trials": int(len(completed)),
        "completed_unique_parameter_points": int(len(completed_unique)),
        "feasible_trials": int(len(feasible)),
        "feasible_unique_parameter_points": int(len(feasible_unique)),
        "unique_feasible_behaviors": total_behaviors,
        "parameter_components": int(len(components)),
        "largest_component_parameter_points": int(len(largest)),
        "largest_component_unique_behaviors": int(largest_behaviors),
        "platform_found": bool(platform_found),
        "boundary_dependent": boundary_dependent,
        "boundary_dependencies": boundary_dependencies,
        "local_feasible_rate_median": float(neighborhoods["local_feasible_rate"].median()),
        "local_feasible_rate_p10": float(neighborhoods["local_feasible_rate"].quantile(0.10)),
        "local_neighborhood_radius_median": float(neighborhoods["neighborhood_radius"].median()),
        "neighborhood_is_retroactive_gate": False,
        "returns_recomputed": False,
        "search_trials_added": 0,
        "configuration_selected": False,
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "platform_evidence.json", evidence)
    dependency_text = "、".join(f"{item['parameter']}({item['side']})" for item in boundary_dependencies) or "无"
    (experiment / "03_execution.md").write_text(
        "# S007 EX23 执行\n\n"
        f"状态：`COMPLETE`。EX22的{len(feasible)}个合格试验包含{len(feasible_unique)}个唯一参数点、"
        f"{total_behaviors}种独立交易行为；参数图形成{len(components)}个连通区域，最大区域覆盖"
        f"{largest_behaviors}种行为。没有读取新收益或追加搜索。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX23 结论\n\n"
        f"裁决：`{decision}`。参数平台判定为`{'PASS' if platform_found else 'FAIL'}`；最大连通区域"
        f"覆盖{len(largest)}个唯一参数点和{largest_behaviors}种交易行为。20近邻可行率中位数/P10为"
        f"{float(neighborhoods['local_feasible_rate'].median()):.2%}/"
        f"{float(neighborhoods['local_feasible_rate'].quantile(0.10)):.2%}。边界依赖：{dependency_text}。"
        "本轮未选择配置或创建候选，需先与用户评审。\n",
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
