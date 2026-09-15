from __future__ import annotations

import hashlib
import importlib.util
import json
import os
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


EXPERIMENT_ID = "20260915_S007_EX26"


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


def _pareto(frame: pd.DataFrame, columns: list[str]) -> pd.Series:
    values = frame[columns].to_numpy(dtype=float)
    keep = np.ones(len(frame), dtype=bool)
    for index, current in enumerate(values):
        dominates = np.all(values >= current, axis=1) & np.any(values > current, axis=1)
        dominates[index] = False
        if dominates.any():
            keep[index] = False
    return pd.Series(keep, index=frame.index)


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


def _audit_platform(
    enriched: pd.DataFrame,
    bounds: dict[str, object],
    protocol: dict[str, object],
    artifacts: Path,
) -> dict[str, object]:
    bound_map = {
        "risk_context_total": bounds["risk_context_total"],
        "opportunity_share_nonrisk": bounds["opportunity_share_nonrisk"],
        "opp_spx_fraction": bounds["within_role_fraction"],
        "confirm_share_fraction": bounds["within_role_fraction"],
        "risk_shibor_fraction": bounds["within_role_fraction"],
        "entry_quantile": bounds["entry_quantile"],
        "exit_quantile": bounds["exit_quantile"],
        "confirmation_gate_quantile": bounds["confirmation_gate_quantile"],
    }
    parameter_names = list(bound_map)
    completed = enriched.loc[enriched["state"].eq("COMPLETE")].copy()
    feasible = completed.loc[completed["feasible"].eq(True)].copy()
    if completed.empty or feasible.empty:
        return {
            "platform_found": False,
            "boundary_dependent": False,
            "boundary_dependencies": [],
            "completed_unique_parameter_points": int(len(completed)),
            "feasible_unique_parameter_points": 0,
            "unique_feasible_behaviors": 0,
            "parameter_components": 0,
            "largest_component_parameter_points": 0,
            "largest_component_unique_behaviors": 0,
            "local_feasible_rate_median": None,
            "local_feasible_rate_p10": None,
            "local_neighborhood_radius_median": None,
        }

    def with_parameter_hash(frame: pd.DataFrame) -> pd.DataFrame:
        output = frame.copy()
        output["parameter_hash"] = output["params"].map(
            lambda value: hashlib.sha256(json.dumps(json.loads(value), sort_keys=True).encode()).hexdigest()
        )
        return output.sort_values(["objective.calmar", "objective.cagr"], ascending=False).drop_duplicates(
            "parameter_hash"
        ).reset_index(drop=True)

    completed_unique = with_parameter_hash(completed)
    feasible_unique = with_parameter_hash(feasible)

    def normalized_params(frame: pd.DataFrame) -> pd.DataFrame:
        params = pd.DataFrame(frame["params"].map(json.loads).tolist(), index=frame.index)
        normalized = pd.DataFrame(index=frame.index)
        for name, (low, high) in bound_map.items():
            normalized[name] = (params[name].astype(float) - float(low)) / (float(high) - float(low))
        values = normalized.to_numpy(dtype=float)
        if (
            not np.isfinite(values).all()
            or not normalized.ge(-1e-12).all().all()
            or not normalized.le(1.0 + 1e-12).all().all()
        ):
            raise ValueError("normalized parameters are non-finite or outside EX26 bounds")
        return normalized

    feasible_normalized = normalized_params(feasible_unique)
    feasible_values = feasible_normalized.to_numpy(dtype=float)
    radius = float(protocol["platform"]["normalized_linf_radius"])
    distances = np.max(np.abs(feasible_values[:, None, :] - feasible_values[None, :, :]), axis=2)
    groups = _components(distances <= radius)
    group_details: list[tuple[list[int], int]] = []
    component_rows: list[dict[str, object]] = []
    for ordinal, indices in enumerate(groups, start=1):
        group = feasible_unique.iloc[indices]
        unique_behaviors = int(group["behavior_hash"].nunique())
        group_details.append((indices, unique_behaviors))
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
            "trial_ids": json.dumps(group["trial_id"].tolist()),
        })
    components = pd.DataFrame(component_rows).sort_values(
        ["unique_behaviors", "parameter_points"], ascending=False
    ).reset_index(drop=True)
    components.to_csv(artifacts / "parameter_components.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    largest_indices, largest_behaviors = max(group_details, key=lambda item: (item[1], len(item[0])))
    largest = feasible_unique.iloc[largest_indices].copy()
    largest_normalized = feasible_normalized.iloc[largest_indices]
    margin = float(protocol["boundary_diagnostic"]["normalized_boundary_margin"])
    dependency_share = float(protocol["boundary_diagnostic"]["dependency_share"])
    boundary_rows: list[dict[str, object]] = []
    boundary_dependencies: list[dict[str, str]] = []
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
    pd.DataFrame(boundary_rows).to_csv(
        artifacts / "parameter_boundary_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )

    completed_normalized = normalized_params(completed_unique)
    completed_values = completed_normalized.to_numpy(dtype=float)
    completed_feasible = completed_unique["feasible"].astype(bool).to_numpy()
    k = int(protocol["neighborhood_diagnostic"]["nearest_parameter_points"])
    neighborhood_rows: list[dict[str, object]] = []
    for index, values in enumerate(feasible_values):
        local_distances = np.max(np.abs(completed_values - values), axis=1)
        nearest = np.argsort(local_distances, kind="stable")[: min(k, len(local_distances))]
        neighborhood_rows.append({
            "trial_id": feasible_unique.iloc[index]["trial_id"],
            "behavior_hash": feasible_unique.iloc[index]["behavior_hash"],
            "nearest_points": int(len(nearest)),
            "neighborhood_radius": float(local_distances[nearest[-1]]),
            "local_feasible_rate": float(completed_feasible[nearest].mean()),
            "local_feasible_points": int(completed_feasible[nearest].sum()),
        })
    neighborhoods = pd.DataFrame(neighborhood_rows)
    neighborhoods.to_csv(
        artifacts / "local_neighborhood_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )

    platform = protocol["platform"]
    total_behaviors = int(feasible["behavior_hash"].nunique())
    platform_found = (
        total_behaviors >= int(platform["minimum_unique_behaviors"])
        and largest_behaviors >= int(platform["minimum_connected_unique_behaviors"])
    )
    return {
        "platform_found": bool(platform_found),
        "boundary_dependent": bool(boundary_dependencies),
        "boundary_dependencies": boundary_dependencies,
        "completed_unique_parameter_points": int(len(completed_unique)),
        "feasible_unique_parameter_points": int(len(feasible_unique)),
        "unique_feasible_behaviors": total_behaviors,
        "parameter_components": int(len(components)),
        "largest_component_parameter_points": int(len(largest)),
        "largest_component_unique_behaviors": int(largest_behaviors),
        "largest_component_cagr_min": float(largest["objective.cagr"].min()),
        "largest_component_cagr_median": float(largest["objective.cagr"].median()),
        "largest_component_cagr_max": float(largest["objective.cagr"].max()),
        "largest_component_maximum_drawdown_worst": float(largest["objective.max_drawdown"].min()),
        "largest_component_maximum_drawdown_median": float(largest["objective.max_drawdown"].median()),
        "largest_component_calmar_median": float(largest["objective.calmar"].median()),
        "largest_component_frequency_median_min": float(largest["metric.full.rolling_60_closed_trades_median"].min()),
        "largest_component_frequency_median_median": float(
            largest["metric.full.rolling_60_closed_trades_median"].median()
        ),
        "largest_component_frequency_p10_median": float(
            largest["metric.full.rolling_60_closed_trades_p10"].median()
        ),
        "local_feasible_rate_median": float(neighborhoods["local_feasible_rate"].median()),
        "local_feasible_rate_p10": float(neighborhoods["local_feasible_rate"].quantile(0.10)),
        "local_neighborhood_radius_median": float(neighborhoods["neighborhood_radius"].median()),
    }


def main() -> None:
    started = time.perf_counter()
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_new_returns"):
        raise ValueError("EX26 protocol identity or return declaration differs")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX26 cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    ex25 = repo / str(sources["ex25_archive"])
    ex24 = repo / str(sources["ex24_archive"])
    ex19 = repo / str(sources["ex19_archive"])
    ex21 = repo / str(sources["ex21_archive"])
    validate_experiment_archive(ex25)
    validate_experiment_archive(ex24)
    validate_experiment_archive(ex19)
    validate_experiment_archive(ex21)
    frozen = {
        ex25 / "experiment_manifest.json": sources["ex25_manifest_sha256"],
        ex25 / "artifacts/ablation_evidence.json": sources["ex25_ablation_evidence_sha256"],
        ex24 / "experiment_manifest.json": sources["ex24_manifest_sha256"],
        ex24 / "artifacts/platform_evidence.json": sources["ex24_platform_evidence_sha256"],
        ex24 / "artifacts/search_trial_ledger.csv.gz": sources["ex24_search_ledger_sha256"],
        ex24 / "artifacts/parameter_components.csv": sources["ex24_parameter_components_sha256"],
        ex19 / "experiment_manifest.json": sources["ex19_manifest_sha256"],
        ex19 / "artifacts/component_contract.json": sources["component_contract_sha256"],
        ex21 / "experiment_manifest.json": sources["ex21_manifest_sha256"],
        ex21 / "artifacts/protocol.json": sources["ex21_protocol_sha256"],
        repo / str(sources["effective_search_protocol"]): sources["effective_search_protocol_sha256"],
        repo / str(sources["feature_panel"]): sources["feature_panel_sha256"],
        repo / str(sources["search_adapter"]): sources["search_adapter_sha256"],
        repo / str(sources["ex09_script"]): sources["ex09_script_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen EX26 source differs: {path}")

    search = protocol["search"]
    workers = int(search["workers"])
    available = os.cpu_count() or 1
    if available != int(search["available_logical_processors"]):
        raise ValueError(f"logical processor count differs: expected={search['available_logical_processors']}, actual={available}")
    if not 1 <= workers <= available:
        raise ValueError("worker count is outside available logical processors")
    if search["storage"] != "IN_MEMORY" or search["method"] != "nsga2":
        raise ValueError("EX26 search engine differs from protocol")

    helpers = _load_helpers(repo / str(sources["ex09_script"]))
    effective = _read(repo / str(sources["effective_search_protocol"]))
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
    bounds = search["parameters"]
    gates = protocol["hard_gates"]
    fee = float(protocol["execution"]["fee_rate_one_way"])
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
        risk_context = float(params["risk_context_total"])
        nonrisk = 1.0 - risk_context
        opportunity_share = float(params["opportunity_share_nonrisk"])
        opportunity = nonrisk * opportunity_share
        entry_timing = nonrisk * (1.0 - opportunity_share)
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
            features["entry_vwap"]: entry_timing,
            features["risk_shibor"]: risk_context * risk_fraction,
            features["risk_range"]: risk_context * (1.0 - risk_fraction),
        }
        if max(weights.values()) > float(bounds["maximum_single_factor_weight"]):
            raise SearchTrialRejected("FACTOR_WEIGHT_TOO_LARGE", "single-factor weight exceeds limit")
        contributions = scores.mul(pd.Series(weights), axis=1)
        combined = contributions.sum(axis=1).where(scores[list(weights)].notna().all(axis=1))
        discovery_values = combined.loc[discovery_mask].dropna()
        if len(discovery_values) < 300:
            raise SearchTrialRejected("INSUFFICIENT_DISCOVERY_SCORE", "too few discovery scores")
        entry_threshold = float(discovery_values.quantile(entry_quantile))
        exit_threshold = float(discovery_values.quantile(exit_quantile))
        if exit_threshold >= entry_threshold:
            raise SearchTrialRejected("INVALID_THRESHOLDS", "exit threshold is not below entry threshold")

        base_decision = helpers._hysteresis(combined, entry_threshold, exit_threshold)
        base_entries = base_decision.gt(base_decision.shift(1, fill_value=0.0)) & discovery_mask
        confirmation_score = (
            scores[features["confirm_share"]] * confirm_fraction
            + scores[features["confirm_volume"]] * (1.0 - confirm_fraction)
        )
        threshold_source = confirmation_score.loc[base_entries].dropna()
        if len(threshold_source) < 20:
            raise SearchTrialRejected("INSUFFICIENT_GATE_SOURCE", "too few discovery entries for confirmation gate")
        gate_quantile = float(params["confirmation_gate_quantile"])
        confirmation_threshold = float(threshold_source.quantile(gate_quantile))
        decision = _gated_hysteresis(
            combined,
            confirmation_score,
            entry_threshold,
            exit_threshold,
            confirmation_threshold,
        )
        target = decision.shift(1).fillna(0.0)
        full = helpers._metrics(target, prices, fee)
        discovery = helpers._metrics(target.loc[discovery_mask], prices.loc[discovery_mask], fee)
        confirmation_metrics = helpers._metrics(target.loc[confirmation_mask], prices.loc[confirmation_mask], fee)
        payload = {
            "params": params,
            "weights": weights,
            "role_totals": {
                "opportunity": opportunity,
                "entry_timing": entry_timing,
                "risk_context": risk_context,
                "confirmation_base_score": 0.0,
            },
            "entry_threshold": entry_threshold,
            "exit_threshold": exit_threshold,
            "confirmation_threshold": confirmation_threshold,
        }
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
                "discovery_cagr_shortfall": float(gates["minimum_discovery_cagr"]) - float(discovery["cagr"]),
                "confirmation_cagr_shortfall": float(gates["minimum_confirmation_cagr"]) - float(confirmation_metrics["cagr"]),
                "confirmation_drawdown_loss": abs(min(float(confirmation_metrics["maximum_drawdown"]), 0.0)),
            },
            strategy_hash=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
            behavior_hash=hashlib.sha256(target.to_numpy(dtype=np.int8).tobytes()).hexdigest(),
            metadata={
                "weights": weights,
                "role_totals": payload["role_totals"],
                "entry_threshold": entry_threshold,
                "exit_threshold": exit_threshold,
                "confirmation_threshold": confirmation_threshold,
                "gate_source_entries": int(len(threshold_source)),
                "fee_rate_one_way": fee,
                "full": full,
                "discovery": discovery,
                "confirmation": confirmation_metrics,
            },
        )

    low_fraction, high_fraction = map(float, bounds["within_role_fraction"])
    spec = SearchSpec(
        study_name="S007-EX26-DECOUPLED-GATED-SCORE",
        method="nsga2",
        parameters=(
            FloatParameter("risk_context_total", *map(float, bounds["risk_context_total"])),
            FloatParameter(
                "opportunity_share_nonrisk",
                *map(float, bounds["opportunity_share_nonrisk"]),
            ),
            FloatParameter("opp_spx_fraction", low_fraction, high_fraction),
            FloatParameter("confirm_share_fraction", low_fraction, high_fraction),
            FloatParameter("risk_shibor_fraction", low_fraction, high_fraction),
            FloatParameter("entry_quantile", *map(float, bounds["entry_quantile"])),
            FloatParameter("exit_quantile", *map(float, bounds["exit_quantile"])),
            FloatParameter("confirmation_gate_quantile", *map(float, bounds["confirmation_gate_quantile"])),
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
            ConstraintSpec("discovery_cagr_shortfall", 0.0),
            ConstraintSpec("confirmation_cagr_shortfall", 0.0),
            ConstraintSpec("confirmation_drawdown_loss", abs(float(gates["confirmation_maximum_drawdown_limit"]))),
        ),
        target_trials=int(search["target_trials"]),
        seed=int(search["seed"]),
        storage_path=None,
        workers=workers,
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

    completed = enriched.loc[enriched["state"].eq("COMPLETE")].sort_values(["objective.calmar", "objective.cagr"], ascending=False)
    best_completed = completed.iloc[0]
    best_feasible = feasible.sort_values(["objective.calmar", "objective.cagr"], ascending=False).iloc[0] if len(feasible) else None
    unique_feasible = int(feasible["behavior_hash"].nunique()) if len(feasible) else 0
    platform_audit = _audit_platform(enriched, bounds, protocol, artifacts)
    ex24_components = pd.read_csv(ex24 / "artifacts/parameter_components.csv")
    ex24_largest = ex24_components.sort_values(
        ["unique_behaviors", "parameter_points"], ascending=False
    ).iloc[0]
    ex24_trial_ids = json.loads(str(ex24_largest["trial_ids"]))
    ex24_ledger = pd.read_csv(ex24 / "artifacts/search_trial_ledger.csv.gz").set_index("trial_id")
    ex24_platform = ex24_ledger.loc[ex24_trial_ids]
    reference = {
        "cagr_median": float(ex24_platform["objective.cagr"].median()),
        "maximum_drawdown_median": float(ex24_platform["objective.max_drawdown"].median()),
        "calmar_median": float(ex24_platform["objective.calmar"].median()),
        "frequency_median_median": float(
            ex24_platform["metric.full.rolling_60_closed_trades_median"].median()
        ),
        "frequency_p10_median": float(
            ex24_platform["metric.full.rolling_60_closed_trades_p10"].median()
        ),
        "parameter_points": int(len(ex24_platform)),
        "unique_behaviors": int(ex24_platform["behavior_hash"].nunique()),
    }
    decoupled = {
        "cagr_median": platform_audit.get("largest_component_cagr_median"),
        "maximum_drawdown_median": platform_audit.get(
            "largest_component_maximum_drawdown_median"
        ),
        "calmar_median": platform_audit.get("largest_component_calmar_median"),
        "frequency_median_median": platform_audit.get(
            "largest_component_frequency_median_median"
        ),
        "frequency_p10_median": platform_audit.get(
            "largest_component_frequency_p10_median"
        ),
        "parameter_points": platform_audit["largest_component_parameter_points"],
        "unique_behaviors": platform_audit["largest_component_unique_behaviors"],
    }
    comparison_deltas = {
        key: float(decoupled[key]) - float(reference[key])
        for key in ("cagr_median", "maximum_drawdown_median", "calmar_median")
        if decoupled[key] is not None
    }
    all_not_worse = bool(comparison_deltas) and all(value >= 0.0 for value in comparison_deltas.values())
    all_not_better = bool(comparison_deltas) and all(value <= 0.0 for value in comparison_deltas.values())
    decision = (
        "STOP_NO_STABLE_DECOUPLED_PLATFORM"
        if not platform_audit["platform_found"]
        else "REVIEW_DECOUPLED_BOUNDARY_EXTENSION_REQUIRED"
        if platform_audit["boundary_dependent"]
        else "PREFER_DECOUPLED_PROTOTYPE_REVIEW"
        if all_not_worse
        else "RETAIN_EX24_PROTOTYPE_REVIEW"
        if all_not_better
        else "REVIEW_ARCHITECTURE_TRADEOFF"
    )
    elapsed = time.perf_counter() - started

    def summary(row: pd.Series | None) -> dict[str, object] | None:
        if row is None:
            return None
        return {
            "trial_id": str(row["trial_id"]),
            "cagr": float(row["objective.cagr"]),
            "maximum_drawdown": float(row["objective.max_drawdown"]),
            "calmar": float(row["objective.calmar"]),
            "closed_trades": int(row["metric.full.closed_trades"]),
            "frequency_median": float(row["metric.full.rolling_60_closed_trades_median"]),
            "frequency_p10_observation": float(row["metric.full.rolling_60_closed_trades_p10"]),
            "discovery_cagr": float(row["metric.discovery.cagr"]),
            "confirmation_cagr": float(row["metric.confirmation.cagr"]),
        }

    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": decision,
        "runtime_seconds": round(elapsed, 3),
        "trials_per_second": round(len(ledger) / elapsed, 3),
        "logical_processors": available,
        "workers": workers,
        "target_trials": result.target_trials,
        "completed_trials": result.completed_trials,
        "pruned_trials": result.pruned_trials,
        "failed_trials": result.failed_trials,
        "feasible_trials": result.feasible_trials,
        "unique_feasible_behaviors": unique_feasible,
        "pareto_trials": int(len(pareto)),
        "best_completed": summary(best_completed),
        "best_feasible": summary(best_feasible),
        "p10_is_observation_only": True,
        "platform_audited": True,
        "platform": platform_audit,
        "prototype_comparison": {
            "reference_ex24": reference,
            "decoupled_ex26": decoupled,
            "decoupled_minus_reference": comparison_deltas,
            "all_performance_medians_not_worse": all_not_worse,
            "all_performance_medians_not_better": all_not_better,
        },
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "search_evidence.json", evidence)
    _write(artifacts / "platform_evidence.json", {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": decision,
        **platform_audit,
        "neighborhood_is_retroactive_gate": False,
        "configuration_selected": False,
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    })
    _write(artifacts / "prototype_comparison.json", {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "reference_ex24": reference,
        "decoupled_ex26": decoupled,
        "decoupled_minus_reference": comparison_deltas,
        "all_performance_medians_not_worse": all_not_worse,
        "all_performance_medians_not_better": all_not_better,
        "decision": decision,
    })
    (experiment / "03_execution.md").write_text(
        "# S007 EX26 执行\n\n"
        f"状态：`COMPLETE`。8工作线程在{elapsed:.2f}秒内完成{result.completed_trials}个有效评估，"
        f"结构剪枝{result.pruned_trials}个、失败{result.failed_trials}个；满足全部硬门"
        f"{result.feasible_trials}个，共{unique_feasible}种独立交易行为。同轮完成参数平台和边界审计。\n",
        encoding="utf-8",
    )
    feasible_text = (
        "没有试验满足全部硬门"
        if best_feasible is None
        else f"最佳合格试验年化{float(best_feasible['objective.cagr']):.2%}、最大回撤"
        f"{float(best_feasible['objective.max_drawdown']):.2%}、卡玛{float(best_feasible['objective.calmar']):.2f}、"
        f"频率中位数/P10为{float(best_feasible['metric.full.rolling_60_closed_trades_median']):.1f}/"
        f"{float(best_feasible['metric.full.rolling_60_closed_trades_p10']):.1f}"
    )
    dependencies = platform_audit["boundary_dependencies"]
    dependency_text = "、".join(f"{item['parameter']}({item['side']})" for item in dependencies) or "无"
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX26 结论\n\n"
        f"裁决：`{decision}`。{feasible_text}。解耦原型最大平台覆盖"
        f"{platform_audit['largest_component_parameter_points']}个唯一参数点和"
        f"{platform_audit['largest_component_unique_behaviors']}种交易行为，边界依赖：{dependency_text}。"
        f"相对EX24主平台，年化、最大回撤和卡玛中位数变化为"
        f"{comparison_deltas.get('cagr_median', 0.0):+.2%}/"
        f"{comparison_deltas.get('maximum_drawdown_median', 0.0):+.2%}/"
        f"{comparison_deltas.get('calmar_median', 0.0):+.3f}。"
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
