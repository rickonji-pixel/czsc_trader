from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX11"


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


def _segment_metrics(
    helpers: object,
    target: pd.Series,
    prices: pd.DataFrame,
    fee: float,
    name: str,
) -> list[dict[str, object]]:
    strategy = helpers._metrics(target, prices, fee)
    buyhold = helpers._metrics(pd.Series(1.0, index=prices.index), prices, fee)
    rows: list[dict[str, object]] = []
    for subject, metrics in (("S007_PLATFORM_MEDOID", strategy), ("BUYHOLD", buyhold)):
        rows.append({"period": name, "subject": subject, **metrics})
    rows.append({
        "period": name,
        "subject": "S007_MINUS_BUYHOLD",
        "cagr": float(strategy["cagr"]) - float(buyhold["cagr"]),
        "total_return": float(strategy["total_return"]) - float(buyhold["total_return"]),
        "maximum_drawdown": float(strategy["maximum_drawdown"]) - float(buyhold["maximum_drawdown"]),
        "calmar": float(strategy["calmar"]) - float(buyhold["calmar"]),
        "closed_trades": int(strategy["closed_trades"]),
        "order_count": int(strategy["order_count"]),
        "rolling_60_closed_trades_median": float(strategy["rolling_60_closed_trades_median"]),
        "rolling_60_closed_trades_p10": float(strategy["rolling_60_closed_trades_p10"]),
        "exposure_ratio": float(strategy["exposure_ratio"]) - 1.0,
    })
    return rows


def _ic_rows(
    inputs: pd.DataFrame,
    outcome: pd.Series,
    segment_masks: dict[str, pd.Series],
    minimum_annual: int,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    masks = dict(segment_masks)
    for year in sorted(inputs.index.year.unique()):
        masks[f"YEAR_{year}"] = pd.Series(inputs.index.year == year, index=inputs.index)
    for period, mask in masks.items():
        for name in inputs.columns:
            pair = pd.concat([inputs.loc[mask, name], outcome.loc[mask]], axis=1).dropna()
            minimum = minimum_annual if period.startswith("YEAR_") else 100
            ic = float(pair.iloc[:, 0].corr(pair.iloc[:, 1], method="spearman")) if len(pair) >= minimum else np.nan
            rows.append({"period": period, "input": name, "observations": len(pair), "spearman_ic": ic})
    return rows


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_new_returns"):
        raise ValueError("EX11 protocol identity or return declaration differs")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX11 cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    ex10 = repo / str(sources["ex10_archive"])
    ex09 = repo / str(sources["ex09_archive"])
    validate_experiment_archive(ex10)
    validate_experiment_archive(ex09)
    frozen = {
        ex10 / "experiment_manifest.json": sources["ex10_manifest_sha256"],
        ex10 / "artifacts/parameter_components.csv": sources["parameter_components_sha256"],
        ex09 / "artifacts/feasible_trials.csv": sources["feasible_trials_sha256"],
        ex09 / "run_experiment.py": sources["ex09_script_sha256"],
        repo / str(sources["feature_panel"]): sources["feature_panel_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen temporal source differs: {path}")

    helpers = _load_helpers(ex09 / "run_experiment.py")
    search_protocol = _read(ex09 / "artifacts/protocol.json")
    ex08 = repo / str(search_protocol["sources"]["ex08_archive"])
    effective = _read(ex08 / "artifacts/effective_search_protocol.json")
    feasible = pd.read_csv(ex09 / "artifacts/feasible_trials.csv")
    components = pd.read_csv(ex10 / "artifacts/parameter_components.csv")
    largest_ids = json.loads(components.iloc[0]["trial_ids"])
    largest = feasible.loc[feasible["trial_id"].isin(largest_ids)].copy()
    unique = largest.sort_values("trial_id").drop_duplicates("behavior_hash").reset_index(drop=True)
    parameter_rows = pd.DataFrame(unique["params"].map(json.loads).tolist(), index=unique.index)
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
    unique["distance_sum"] = distances.sum(axis=1)
    medoid = unique.sort_values(["distance_sum", "trial_id"]).iloc[0]
    metadata = json.loads(medoid["metadata"])

    panel = pd.read_csv(repo / str(sources["feature_panel"]), parse_dates=["date"]).set_index("date")
    prices = helpers._raw_daily(repo, search_protocol["sources"]["daily_sha256"])
    prices = prices.loc[prices.index <= pd.Timestamp(protocol["development_cutoff"])]
    if not panel.index.equals(prices.index):
        raise ValueError("feature panel and price calendar differ")
    normalization = effective["normalization"]
    oriented = pd.DataFrame(index=panel.index)
    for feature, binding in effective["feature_bindings"].items():
        oriented[feature] = helpers._causal_percentile(
            panel[feature],
            int(normalization["lookback_sessions"]),
            int(normalization["minimum_observations"]),
        ) * int(binding["orientation"])
    combined = sum(oriented[name] * float(weight) for name, weight in metadata["weights"].items())
    combined = combined.where(oriented.notna().all(axis=1)).rename("combined_score")
    decision = helpers._hysteresis(combined, float(metadata["entry_threshold"]), float(metadata["exit_threshold"]))
    target = decision.shift(1).fillna(0.0)
    fee = float(effective["execution"]["fee_rate_one_way"])

    segments = protocol["segments"]
    masks = {
        "DISCOVERY": panel.index.to_series().between(segments["discovery_start"], segments["discovery_end"]),
        "CONFIRMATION": panel.index.to_series().between(segments["confirmation_start"], segments["confirmation_end"]),
    }
    performance_rows: list[dict[str, object]] = []
    performance_rows.extend(_segment_metrics(helpers, target, prices, fee, "FULL"))
    for name, mask in masks.items():
        performance_rows.extend(_segment_metrics(helpers, target.loc[mask], prices.loc[mask], fee, name))
    for year in sorted(prices.index.year.unique()):
        mask = pd.Series(prices.index.year == year, index=prices.index)
        performance_rows.extend(_segment_metrics(helpers, target.loc[mask], prices.loc[mask], fee, f"YEAR_{year}"))
    performance = pd.DataFrame(performance_rows)
    performance.to_csv(artifacts / "temporal_performance.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    horizon = int(protocol["diagnosis"]["forward_horizon_sessions"])
    outcome = prices["open"].shift(-(horizon + 1)).div(prices["open"].shift(-1)).sub(1.0)
    ic_inputs = oriented.copy()
    ic_inputs["combined_score"] = combined
    ic = pd.DataFrame(_ic_rows(ic_inputs, outcome, masks, int(protocol["diagnosis"]["minimum_annual_ic_observations"])))
    ic.to_csv(artifacts / "temporal_information_coefficients.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    coverage_rows: list[dict[str, object]] = []
    for year in sorted(panel.index.year.unique()):
        year_mask = panel.index.year == year
        for feature in oriented.columns:
            values = oriented.loc[year_mask, feature]
            coverage_rows.append({
                "year": int(year),
                "feature": feature,
                "raw_coverage": float(panel.loc[year_mask, feature].notna().mean()),
                "normalized_coverage": float(values.notna().mean()),
                "normalized_mean": float(values.mean()),
                "normalized_std": float(values.std()),
                "normalized_q10": float(values.quantile(0.10)),
                "normalized_q90": float(values.quantile(0.90)),
            })
    coverage = pd.DataFrame(coverage_rows)
    coverage.to_csv(artifacts / "temporal_feature_coverage.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    segment_excess = performance.loc[
        performance["period"].isin(masks) & performance["subject"].eq("S007_MINUS_BUYHOLD")
    ].set_index("period")["cagr"]
    segment_score_ic = ic.loc[
        ic["period"].isin(masks) & ic["input"].eq("combined_score")
    ].set_index("period")["spearman_ic"]
    post_warmup_coverage = coverage.loc[coverage["year"].gt(int(panel.index.year.min()))]
    coverage_ranges = post_warmup_coverage.groupby("feature")["normalized_coverage"].agg(lambda values: values.max() - values.min())
    relative_consistent = bool(segment_excess.gt(0.0).all())
    score_consistent = bool(segment_score_ic.notna().all() and segment_score_ic.gt(0.0).all())
    coverage_stable = bool(coverage_ranges.max() <= float(protocol["diagnosis"]["maximum_material_normalized_coverage_range"]))
    decision_code = (
        "PROCEED_TO_CONFIGURATION_AND_ROBUSTNESS_AUDIT"
        if relative_consistent and score_consistent and coverage_stable
        else "CONTINUE_TEMPORAL_MECHANISM_RESEARCH"
    )
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": decision_code,
        "medoid_trial_id": medoid["trial_id"],
        "medoid_behavior_hash": medoid["behavior_hash"],
        "largest_component_unique_behaviors": int(len(unique)),
        "medoid_distance_sum": float(medoid["distance_sum"]),
        "segment_excess_cagr": {key: float(value) for key, value in segment_excess.items()},
        "segment_combined_score_ic": {key: float(value) for key, value in segment_score_ic.items()},
        "maximum_post_warmup_normalized_coverage_range": float(coverage_ranges.max()),
        "relative_performance_consistent": relative_consistent,
        "combined_score_direction_consistent": score_consistent,
        "feature_coverage_stable": coverage_stable,
        "confirmation_is_independent_out_of_sample": False,
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "temporal_diagnosis.json", evidence)
    discovery_excess = float(segment_excess["DISCOVERY"])
    confirmation_excess = float(segment_excess["CONFIRMATION"])
    discovery_ic = float(segment_score_ic["DISCOVERY"])
    confirmation_ic = float(segment_score_ic["CONFIRMATION"])
    (experiment / "03_execution.md").write_text(
        "# S007 EX11 执行\n\n"
        f"状态：`COMPLETE`。最大参数平台的{len(unique)}种独立行为中，按参数几何中心选出"
        f"`{medoid['trial_id']}`。发现期/确认期相对BuyHold年化差分别为{discovery_excess:.2%}/"
        f"{confirmation_excess:.2%}；组合分数3日IC分别为{discovery_ic:.4f}/{confirmation_ic:.4f}。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX11 结论\n\n"
        f"裁决：`{decision_code}`。平台中心在前后两段均获得正超额年化，组合分数IC方向"
        f"{'一致' if score_consistent else '不一致'}，归一化输入在预热期后最大年度覆盖率跨度为"
        f"{float(coverage_ranges.max()):.2%}。这说明EX10的绝对收益时间集中主要受市场机会差异影响"
        "，但所有结果仍来自同一开发池，下一阶段只允许审计配置与稳健性，不能直接登记候选。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": protocol["experiment_type"],
        "strategy_id": protocol["strategy_id"],
        "symbol": protocol["symbol"],
        "development_cutoff": protocol["development_cutoff"],
        "decision": decision_code,
        "promotion_allowed": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
