from __future__ import annotations

from dataclasses import asdict
import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
from strategy_evaluator import (
    ReturnMatrixEvidence,
    annualized_sharpe,
    calculate_dsr_bundle,
    cscv_pbo,
    effective_trial_count,
    hash_return_matrix,
    paired_stationary_bootstrap,
    performance_metrics,
    stationary_bootstrap_performance,
)

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX29"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _portfolio_returns(target: pd.Series, prices: pd.DataFrame, fee: float) -> pd.Series:
    desired = target.reindex(prices.index).fillna(0.0).astype(float)
    if not desired.isin([0.0, 1.0]).all():
        raise ValueError("execution target must be binary")
    cash = 1.0
    shares = 0.0
    previous = 0.0
    values = np.empty(len(prices), dtype=float)
    for index, (flag, open_price, close_price) in enumerate(
        zip(
            desired.to_numpy(dtype=float),
            prices["open"].to_numpy(dtype=float),
            prices["close"].to_numpy(dtype=float),
            strict=True,
        )
    ):
        if flag != previous:
            if flag == 1.0:
                shares = cash / (open_price * (1.0 + fee))
                cash = 0.0
            else:
                cash = shares * open_price * (1.0 - fee)
                shares = 0.0
            previous = flag
        values[index] = cash + shares * close_price
    returns = np.empty(len(values), dtype=float)
    returns[0] = values[0] - 1.0
    returns[1:] = values[1:] / values[:-1] - 1.0
    return pd.Series(returns, index=prices.index)


def _label(value: float, favorable: float, adverse: float, *, lower_better: bool) -> str:
    if lower_better:
        if value <= favorable:
            return "FAVORABLE"
        if value > adverse:
            return "ADVERSE"
    else:
        if value >= favorable:
            return "FAVORABLE"
        if value < adverse:
            return "ADVERSE"
    return "MIXED"


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_new_returns"):
        raise ValueError("EX29 protocol identity or return declaration differs")
    forbidden = (
        "appends_search_trials",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX29 may only generate statistical evidence")

    sources = protocol["sources"]
    ex28 = repo / str(sources["ex28_archive"])
    ex27 = repo / str(sources["ex27_archive"])
    validate_experiment_archive(ex28)
    validate_experiment_archive(ex27)
    frozen = {
        ex28 / "experiment_manifest.json": sources["ex28_manifest_sha256"],
        ex28 / "artifacts/selected_configuration.json": sources["selected_configuration_sha256"],
        ex27 / "experiment_manifest.json": sources["ex27_manifest_sha256"],
        ex27 / "artifacts/protocol.json": sources["ex27_protocol_sha256"],
        ex27 / "artifacts/search_trial_ledger.csv.gz": sources["ex27_search_ledger_sha256"],
        repo / str(sources["feature_panel"]): sources["feature_panel_sha256"],
        repo / str(sources["effective_search_protocol"]): sources["effective_search_protocol_sha256"],
        repo / str(sources["ex09_script"]): sources["ex09_script_sha256"],
    }
    for relative, expected in sources["historical_trial_sources"].items():
        frozen[repo / relative] = expected
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen EX29 source differs: {path}")

    ex27_protocol = _read(ex27 / "artifacts/protocol.json")
    helpers = _load_module("s007_ex09_helpers", repo / str(sources["ex09_script"]))
    gated = _load_module("s007_ex27_helpers", ex27 / "run_experiment.py")
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

    selected = _read(ex28 / "artifacts/selected_configuration.json")
    selected_trial_id = str(selected["source_trial_id"])
    selected_behavior_hash = str(selected["behavior_hash"])
    ledger = pd.read_csv(ex27 / "artifacts/search_trial_ledger.csv.gz")
    feasible = ledger.loc[ledger["state"].eq("COMPLETE") & ledger["feasible"].eq(True)].copy()
    feasible["selected_priority"] = feasible["trial_id"].eq(selected_trial_id).astype(int)
    feasible = feasible.sort_values(
        ["selected_priority", "objective.calmar", "objective.cagr"], ascending=[False, False, False]
    ).drop_duplicates("behavior_hash", keep="first")
    feasible = feasible.sort_values("trial_id").reset_index(drop=True)
    if selected_trial_id not in set(feasible["trial_id"]):
        raise ValueError("selected configuration is absent from final feasible behavior universe")

    return_paths: dict[str, pd.Series] = {}
    behavior_rows: list[dict[str, object]] = []
    for _, row in feasible.iterrows():
        trial_id = str(row["trial_id"])
        params = json.loads(str(row["params"]))
        weights = {
            feature: float(row[f"metric.weights.{feature}"])
            for feature in (
                "price_close_vwap_deviation",
                "price_intraday_range",
                "risk_chinext_turnover_z20",
                "risk_global_spx_return",
                "risk_shibor_on_change_5d",
            )
        }
        combined = scores.mul(pd.Series(weights), axis=1).sum(axis=1).where(scores[list(weights)].notna().all(axis=1))
        confirm_share = float(params["confirm_share_fraction"])
        confirmation = (
            scores["micro_share_change_5d_lag1"] * confirm_share
            + scores["tsfresh__log_volume_change__mean__lb20"] * (1.0 - confirm_share)
        )
        decision = gated._gated_hysteresis(
            combined,
            confirmation,
            float(row["metric.entry_threshold"]),
            float(row["metric.exit_threshold"]),
            float(row["metric.confirmation_threshold"]),
        )
        target = decision.shift(1).fillna(0.0)
        computed_hash = hashlib.sha256(target.to_numpy(dtype=np.int8).tobytes()).hexdigest()
        if computed_hash != str(row["behavior_hash"]):
            raise ValueError(f"behavior reproduction failed: {trial_id}")
        return_paths[trial_id] = _portfolio_returns(target, prices, float(protocol["execution"]["fee_rate_one_way"]))
        behavior_rows.append({
            "trial_id": trial_id,
            "behavior_hash": computed_hash,
            "source_trial_number": int(row["trial_number"]),
            "selected": trial_id == selected_trial_id,
        })

    matrix = pd.DataFrame(return_paths, index=prices.index)
    candidate_returns = matrix[selected_trial_id].to_numpy(dtype=float)
    point = performance_metrics(candidate_returns)
    expected = selected["metrics"]
    if not (
        np.isclose(point.cagr, float(expected["cagr"]), atol=1e-12)
        and np.isclose(point.max_drawdown, float(expected["maximum_drawdown"]), atol=1e-12)
    ):
        raise ValueError("selected configuration performance reproduction failed")
    if selected_behavior_hash != next(item["behavior_hash"] for item in behavior_rows if item["selected"]):
        raise ValueError("selected behavior identity differs")

    evidence = ReturnMatrixEvidence(
        tuple(prices.index.strftime("%Y-%m-%d")),
        tuple(matrix.columns),
        tuple(tuple(float(value) for value in row) for row in matrix.to_numpy()),
        "",
    )
    evidence = ReturnMatrixEvidence(evidence.dates, evidence.candidate_ids, evidence.returns, hash_return_matrix(evidence))
    audit = protocol["audit"]
    pbo = cscv_pbo(evidence, int(audit["cscv_blocks"]))
    sharpes = np.asarray([annualized_sharpe(matrix[column].to_numpy()) for column in matrix])
    effective_count = effective_trial_count(matrix.to_numpy())

    inventory: list[dict[str, object]] = []
    raw_trial_upper = 0
    for relative in sources["historical_trial_sources"]:
        frame = pd.read_csv(repo / relative)
        if "search_trial_ledger" in relative:
            counted = len(frame)
            basis = "ALL_SEARCH_PROPOSALS_CONSERVATIVE_UPPER_BOUND"
        elif "local_perturbations" in relative:
            counted = int(frame["state"].eq("COMPLETE").sum())
            basis = "COMPLETED_RETURN_EVALUATIONS"
        else:
            counted = len(frame)
            basis = "RETURN_EVALUATIONS"
        raw_trial_upper += counted
        inventory.append({"source": relative, "rows": len(frame), "counted_trials": counted, "basis": basis})
    dsr = calculate_dsr_bundle(
        candidate_returns,
        sharpes,
        raw_count=raw_trial_upper,
        effective_count=effective_count,
    )

    bootstrap_lengths = tuple(int(value) for value in audit["bootstrap_mean_block_lengths"])
    absolute = [
        stationary_bootstrap_performance(
            candidate_returns,
            candidate_id="S007-C001-PROPOSED",
            repetitions=int(audit["bootstrap_repetitions"]),
            mean_block_length=block_length,
            seed=int(audit["seed"]) + block_length,
        )
        for block_length in bootstrap_lengths
    ]
    buy_hold_target = pd.Series(1.0, index=prices.index)
    buy_hold_returns = _portfolio_returns(
        buy_hold_target, prices, float(protocol["execution"]["fee_rate_one_way"])
    ).to_numpy(dtype=float)
    paired = [
        paired_stationary_bootstrap(
            candidate_returns,
            buy_hold_returns,
            champion_id="S007-C001-PROPOSED",
            comparator_id="BUYHOLD",
            repetitions=int(audit["bootstrap_repetitions"]),
            mean_block_length=block_length,
            seed=int(audit["seed"]) + 1000 + block_length,
        )
        for block_length in bootstrap_lengths
    ]
    primary_absolute = next(item for item in absolute if item.mean_block_length == 21)
    primary_paired = next(item for item in paired if item.mean_block_length == 21)

    pbo_label = _label(
        pbo.pbo,
        float(audit["favorable_pbo_max"]),
        float(audit["adverse_pbo_min_exclusive"]),
        lower_better=True,
    )
    dsr_label = _label(
        dsr.effective.probability,
        float(audit["favorable_dsr_probability_min"]),
        float(audit["adverse_dsr_probability_below"]),
        lower_better=False,
    )
    absolute_label = (
        "FAVORABLE"
        if primary_absolute.cagr.lower_90 > 0.0
        else "MIXED"
        if primary_absolute.cagr.upper_90 > 0.0
        else "ADVERSE"
    )
    paired_probabilities = (
        primary_paired.cagr.probability_favorable,
        primary_paired.max_drawdown.probability_favorable,
        primary_paired.calmar.probability_favorable,
    )
    paired_label = (
        "FAVORABLE"
        if all(value > 0.5 for value in paired_probabilities)
        else "ADVERSE"
        if all(value < 0.5 for value in paired_probabilities)
        else "MIXED"
    )
    neighborhood = selected["selection_evidence"]
    neighborhood_label = (
        "FAVORABLE"
        if float(neighborhood["local_feasible_rate"]) >= 0.80
        and float(neighborhood["minimum_performance_percentile"]) >= 0.50
        and float(neighborhood["parameter_center_linf_distance"]) <= 0.20
        else "ADVERSE"
    )
    labels = {
        "pbo": pbo_label,
        "dsr": dsr_label,
        "absolute_bootstrap": absolute_label,
        "buyhold_paired_bootstrap": paired_label,
        "parameter_neighborhood": neighborhood_label,
    }
    overall = "ADVERSE" if "ADVERSE" in labels.values() else "FAVORABLE" if set(labels.values()) == {"FAVORABLE"} else "MIXED"

    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    matrix.reset_index(names="date").to_csv(
        artifacts / "final_family_daily_return_matrix.csv.gz", index=False, compression=compression
    )
    pd.DataFrame(behavior_rows).to_csv(
        artifacts / "final_family_behavior_ledger.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    pd.DataFrame(inventory).to_csv(
        artifacts / "historical_trial_inventory.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    pd.DataFrame({"trial_id": matrix.columns, "annualized_sharpe": sharpes}).to_csv(
        artifacts / "final_family_trial_sharpes.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    _write(
        artifacts / "return_matrix_identity.json",
        {
            "schema_version": 1,
            "content_hash": evidence.content_hash,
            "date_start": evidence.dates[0],
            "date_end": evidence.dates[-1],
            "observation_count": len(evidence.dates),
            "candidate_count": len(evidence.candidate_ids),
            "candidate_ids": list(evidence.candidate_ids),
            "matrix_artifact": "final_family_daily_return_matrix.csv.gz",
        },
    )
    _write(
        artifacts / "statistical_details.json",
        {
            "pbo": pbo.to_dict(),
            "dsr": {"raw": asdict(dsr.raw), "effective": asdict(dsr.effective)},
            "absolute_bootstrap": [item.to_dict() for item in absolute],
            "buyhold_paired_bootstrap": [item.to_dict() for item in paired],
            "candidate_point_metrics": point.to_dict(),
            "buyhold_point_metrics": performance_metrics(buy_hold_returns).to_dict(),
        },
    )
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "evidence_label": overall,
        "direction_labels": labels,
        "selected_trial_id": selected_trial_id,
        "final_family_feasible_unique_behaviors": len(matrix.columns),
        "conservative_raw_trial_upper_bound": raw_trial_upper,
        "effective_trial_count": effective_count,
        "pbo": pbo.pbo,
        "dsr_raw_probability": dsr.raw.probability,
        "dsr_effective_probability": dsr.effective.probability,
        "bootstrap_21d_cagr_lower_90": primary_absolute.cagr.lower_90,
        "bootstrap_21d_calmar_lower_90": primary_absolute.calmar.lower_90,
        "buyhold_21d_cagr_win_probability": primary_paired.cagr.probability_favorable,
        "buyhold_21d_drawdown_win_probability": primary_paired.max_drawdown.probability_favorable,
        "buyhold_21d_calmar_win_probability": primary_paired.calmar.probability_favorable,
        "candidate_created": False,
        "promotion_allowed": False,
    }
    _write(artifacts / "statistical_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        "# S007 EX29 执行\n\n"
        f"状态：`COMPLETE`。重建EX27的{len(matrix.columns)}种合格独立交易行为，"
        f"并按S007完整历史的{raw_trial_upper}次保守试验上界施加DSR惩罚。"
        "SE完成PBO、DSR、三档绝对区块Bootstrap和相对BuyHold配对区块Bootstrap。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX29 结论\n\n"
        f"总标签：`{overall}`。PBO为{pbo.pbo:.2%}；有效试验数为{effective_count:.2f}；"
        f"有效DSR概率为{dsr.effective.probability:.2%}；21日区块Bootstrap年化收益90%下界为"
        f"{primary_absolute.cagr.lower_90:.2%}、卡玛90%下界为{primary_absolute.calmar.lower_90:.2f}。"
        f"相对BuyHold的年化/回撤/卡玛胜出概率分别为{paired_probabilities[0]:.2%}/"
        f"{paired_probabilities[1]:.2%}/{paired_probabilities[2]:.2%}。\n\n"
        f"方向标签：`{json.dumps(labels, ensure_ascii=False, sort_keys=True)}`。"
        "本轮只形成统计证据，尚未创建候选或执行正式PK。\n",
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
            "evidence_label": overall,
            "candidate_created": False,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
