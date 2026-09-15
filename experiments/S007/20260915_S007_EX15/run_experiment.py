from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX15"


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
    return sorted(groups, key=lambda group: (-len(group), group[0]))


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or protocol.get("reads_new_returns"):
        raise ValueError("EX15 protocol identity or return declaration differs")
    forbidden = ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX15 cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    ex14 = repo / str(sources["ex14_archive"])
    validate_experiment_archive(ex14)
    frozen = {
        ex14 / "experiment_manifest.json": sources["ex14_manifest_sha256"],
        ex14 / "artifacts/search_trial_ledger.csv.gz": sources["search_ledger_sha256"],
        ex14 / "artifacts/feasible_trials.csv": sources["feasible_trials_sha256"],
        ex14 / "run_experiment.py": sources["ex14_script_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen platform source differs: {path}")

    ledger = pd.read_csv(ex14 / "artifacts/search_trial_ledger.csv.gz")
    completed = ledger.loc[ledger["state"].eq("COMPLETE")].copy()
    feasible = completed.loc[completed["feasible"].eq(True)].copy()
    unique = feasible.drop_duplicates("behavior_hash").reset_index(drop=True)

    bounds = {
        "opportunity_total": (0.25, 0.50),
        "confirmation_total": (0.20, 0.45),
        "entry_timing_total": (0.05, 0.25),
        "opp_spx_fraction": (0.25, 0.75),
        "confirm_share_fraction": (0.25, 0.75),
        "risk_shibor_fraction": (0.25, 0.75),
        "entry_quantile": (0.45, 0.80),
        "exit_quantile": (0.20, 0.55),
    }
    if len(unique):
        params = pd.DataFrame(unique["params"].map(json.loads).tolist())
        normalized = pd.DataFrame(index=params.index)
        for name, (low, high) in bounds.items():
            normalized[name] = (params[name] - low) / (high - low)
        values = normalized.to_numpy(dtype="float64")
        distances = np.max(np.abs(values[:, None, :] - values[None, :, :]), axis=2)
        groups = _components(distances <= float(protocol["platform"]["normalized_linf_radius"]))
    else:
        groups = []

    component_rows: list[dict[str, object]] = []
    for ordinal, indices in enumerate(groups, start=1):
        group = unique.iloc[indices]
        component_rows.append({
            "component_id": f"PARAM-COMPONENT-{ordinal:02d}",
            "trials": len(group),
            "unique_behaviors": int(group["behavior_hash"].nunique()),
            "cagr_min": float(group["objective.cagr"].min()),
            "cagr_median": float(group["objective.cagr"].median()),
            "cagr_max": float(group["objective.cagr"].max()),
            "maximum_drawdown_worst": float(group["objective.max_drawdown"].min()),
            "maximum_drawdown_median": float(group["objective.max_drawdown"].median()),
            "trial_ids": json.dumps(group["trial_id"].tolist()),
        })
    components = pd.DataFrame(component_rows)
    components.to_csv(artifacts / "parameter_components.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    constraint_columns = [name for name in completed.columns if name.startswith("constraint_violation.")]
    near_miss_rows = []
    for name in constraint_columns:
        values = pd.to_numeric(completed[name], errors="coerce").fillna(float("inf"))
        near_miss_rows.append({
            "constraint": name.removeprefix("constraint_violation."),
            "passing_trials": int(values.le(0).sum()),
            "pass_rate": float(values.le(0).mean()),
            "median_violation": float(values.replace(float("inf"), np.nan).median()),
        })
    constraint_summary = pd.DataFrame(near_miss_rows).sort_values("pass_rate")
    constraint_summary.to_csv(artifacts / "constraint_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    platform = protocol["platform"]
    largest = max((len(group) for group in groups), default=0)
    platform_found = (
        len(unique) >= int(platform["minimum_unique_behaviors"])
        and largest >= int(platform["minimum_connected_unique_behaviors"])
    )
    decision = "PROCEED_TO_CONFIGURATION_REVIEW" if platform_found else "STOP_NO_STABLE_10BP_PARAMETER_PLATFORM"
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": decision,
        "completed_trials": int(len(completed)),
        "feasible_trials": int(len(feasible)),
        "unique_feasible_behaviors": int(len(unique)),
        "parameter_components": int(len(groups)),
        "largest_component_unique_behaviors": int(largest),
        "platform_found": bool(platform_found),
        "minimum_unique_behaviors": int(platform["minimum_unique_behaviors"]),
        "minimum_connected_unique_behaviors": int(platform["minimum_connected_unique_behaviors"]),
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "platform_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S007 EX15 执行\n\n"
        f"状态：`COMPLETE`。EX14的{len(completed)}个有效评估中仅{len(feasible)}个满足全部硬门，"
        f"去重后为{len(unique)}种交易行为；最大参数连通区域覆盖{largest}种行为。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX15 结论\n\n"
        f"裁决：`{decision}`。10bp成本下合格解未达到至少{int(platform['minimum_unique_behaviors'])}种交易行为、"
        f"最大连通区域至少{int(platform['minimum_connected_unique_behaviors'])}种行为的平台门槛，"
        "现有单点属于孤立可行解，不能进入配置评审或候选立项。\n",
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
