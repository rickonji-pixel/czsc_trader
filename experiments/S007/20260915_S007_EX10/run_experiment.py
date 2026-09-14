from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX10"


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
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_new_returns"):
        raise ValueError("EX10 protocol identity or return declaration differs")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX10 cannot create, promote, or deploy a candidate")
    sources = protocol["sources"]
    ex09 = repo / str(sources["ex09_archive"])
    validate_experiment_archive(ex09)
    frozen = {
        ex09 / "experiment_manifest.json": sources["ex09_manifest_sha256"],
        ex09 / "artifacts/search_trial_ledger.csv.gz": sources["search_ledger_sha256"],
        ex09 / "artifacts/feasible_trials.csv": sources["feasible_trials_sha256"],
        ex09 / "run_experiment.py": sources["ex09_script_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen platform source differs: {path}")

    helpers = _load_helpers(ex09 / "run_experiment.py")
    search_protocol = _read(ex09 / "artifacts/protocol.json")
    ex08 = repo / str(search_protocol["sources"]["ex08_archive"])
    effective = _read(ex08 / "artifacts/effective_search_protocol.json")
    panel = pd.read_csv(repo / str(search_protocol["sources"]["feature_panel"]), parse_dates=["date"]).set_index("date")
    prices = helpers._raw_daily(repo, search_protocol["sources"]["daily_sha256"])
    prices = prices.loc[prices.index <= pd.Timestamp(protocol["development_cutoff"])]
    normalization = effective["normalization"]
    scores = pd.DataFrame(index=panel.index)
    for feature, binding in effective["feature_bindings"].items():
        scores[feature] = helpers._causal_percentile(
            panel[feature],
            int(normalization["lookback_sessions"]),
            int(normalization["minimum_observations"]),
        ) * int(binding["orientation"])

    feasible = pd.read_csv(ex09 / "artifacts/feasible_trials.csv")
    if feasible.empty or not feasible["feasible"].eq(True).all():
        raise ValueError("EX09 feasible ledger is empty or contains non-feasible trials")
    parameter_rows = pd.DataFrame(feasible["params"].map(json.loads).tolist(), index=feasible.index)
    bounds = effective["search"]["parameters"]
    bound_map = {
        "opportunity_total": bounds["opportunity_total"],
        "confirmation_total": bounds["confirmation_total"],
        "entry_timing_total": bounds["entry_timing_total"],
        "opp_spx_fraction": bounds["within_role_fraction"],
        "confirm_share_fraction": bounds["within_role_fraction"],
        "risk_shibor_fraction": bounds["within_role_fraction"],
        "entry_quantile": bounds["entry_quantile"],
        "exit_quantile": bounds["exit_quantile"],
    }
    normalized = pd.DataFrame(index=parameter_rows.index)
    for name, (low, high) in bound_map.items():
        normalized[name] = (parameter_rows[name] - float(low)) / (float(high) - float(low))
    distances = np.max(np.abs(normalized.to_numpy()[:, None, :] - normalized.to_numpy()[None, :, :]), axis=2)
    adjacency = distances <= float(protocol["platform"]["normalized_linf_radius"])
    groups = _components(adjacency)
    component_rows: list[dict[str, object]] = []
    for ordinal, indices in enumerate(groups, start=1):
        group = feasible.iloc[indices]
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

    unique = feasible.sort_values(["objective.cagr", "objective.calmar"], ascending=False).drop_duplicates("behavior_hash")
    yearly_rows: list[dict[str, object]] = []
    behavior_rows: list[dict[str, object]] = []
    fee = float(effective["execution"]["fee_rate_one_way"])
    for _, row in unique.iterrows():
        metadata = json.loads(row["metadata"])
        combined = sum(scores[name] * float(weight) for name, weight in metadata["weights"].items()).where(scores.notna().all(axis=1))
        decision = helpers._hysteresis(combined, float(metadata["entry_threshold"]), float(metadata["exit_threshold"]))
        execution_target = decision.shift(1).fillna(0.0)
        positive_years = 0
        for year in sorted(prices.index.year.unique()):
            mask = prices.index.year == year
            metrics = helpers._metrics(execution_target.loc[mask], prices.loc[mask], fee)
            positive_years += int(float(metrics["cagr"]) > 0)
            yearly_rows.append({"behavior_hash": row["behavior_hash"], "trial_id": row["trial_id"], "year": int(year), **metrics})
        full_cagr = float(row["objective.cagr"])
        discovery_cagr = float(metadata["discovery"]["cagr"])
        confirmation_cagr = float(metadata["confirmation"]["cagr"])
        balance_ratio = min(max(discovery_cagr, 0.0), max(confirmation_cagr, 0.0)) / max(full_cagr, 1e-12)
        behavior_rows.append({
            "behavior_hash": row["behavior_hash"],
            "representative_trial_id": row["trial_id"],
            "full_cagr": full_cagr,
            "full_maximum_drawdown": float(row["objective.max_drawdown"]),
            "full_calmar": float(row["objective.calmar"]),
            "discovery_cagr": discovery_cagr,
            "confirmation_cagr": confirmation_cagr,
            "temporal_balance_ratio": balance_ratio,
            "positive_years": positive_years,
        })
    yearly = pd.DataFrame(yearly_rows)
    behaviors = pd.DataFrame(behavior_rows).sort_values(["full_cagr", "full_calmar"], ascending=False)
    yearly.to_csv(artifacts / "annual_behavior_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    behaviors.to_csv(artifacts / "unique_behavior_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    platform = protocol["platform"]
    platform_found = (
        len(behaviors) >= int(platform["minimum_unique_behaviors"])
        and int(components.iloc[0]["unique_behaviors"]) >= int(platform["minimum_connected_unique_behaviors"])
    )
    temporal = protocol["temporal_diagnostic"]
    attention = (
        float(behaviors["temporal_balance_ratio"].median()) < float(temporal["balance_ratio_attention_below"])
        or int((behaviors["positive_years"] >= int(temporal["minimum_positive_years"])).sum()) < len(behaviors) / 2
    )
    decision = (
        "PROCEED_TO_TEMPORAL_MECHANISM_DIAGNOSIS"
        if platform_found and attention
        else "PROCEED_TO_PROVISIONAL_CONFIGURATION_REVIEW"
        if platform_found
        else "STOP_NO_STABLE_PARAMETER_PLATFORM"
    )
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": decision,
        "feasible_trials": int(len(feasible)),
        "unique_behaviors": int(len(behaviors)),
        "parameter_components": int(len(components)),
        "largest_component_trials": int(components.iloc[0]["trials"]),
        "largest_component_unique_behaviors": int(components.iloc[0]["unique_behaviors"]),
        "platform_found": bool(platform_found),
        "median_temporal_balance_ratio": float(behaviors["temporal_balance_ratio"].median()),
        "behaviors_with_minimum_positive_years": int((behaviors["positive_years"] >= int(temporal["minimum_positive_years"])).sum()),
        "temporal_attention": bool(attention),
        "temporal_diagnostic_is_retroactive_gate": False,
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "platform_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        f"# S007 EX10 执行\n\n状态：`COMPLETE`。54个合格试验去重为{len(behaviors)}种交易行为；参数图形成{len(components)}个连通区域，最大区域覆盖{int(components.iloc[0]['unique_behaviors'])}种行为。逐行为年度绩效已重算。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        f"# S007 EX10 结论\n\n裁决：`{decision}`。参数平台判定为`{'PASS' if platform_found else 'FAIL'}`，但发现/确认期收益平衡比中位数仅{float(behaviors['temporal_balance_ratio'].median()):.2%}。该时间集中度属于事后诊断，不追溯否定EX09硬门；下一步应解释2024年前后差异，暂不形成候选。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {"experiment_id": EXPERIMENT_ID, "status": "COMPLETE", "experiment_type": protocol["experiment_type"], "strategy_id": protocol["strategy_id"], "symbol": protocol["symbol"], "development_cutoff": protocol["development_cutoff"], "decision": decision, "promotion_allowed": False})
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
