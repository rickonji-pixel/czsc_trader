from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX13"


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


def _minimum_holding_target(raw_target: pd.Series, sessions: int) -> pd.Series:
    output = raw_target.copy().astype(float)
    current = 0.0
    entry_index = -sessions
    for index, desired in enumerate(raw_target.to_numpy(dtype=float)):
        if current == 0.0 and desired == 1.0:
            current = 1.0
            entry_index = index
        elif current == 1.0 and desired == 0.0 and index - entry_index >= sessions:
            current = 0.0
        output.iloc[index] = current
    return output


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_new_returns"):
        raise ValueError("EX13 protocol identity or return declaration differs")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX13 cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    ex12 = repo / str(sources["ex12_archive"])
    ex09 = repo / str(sources["ex09_archive"])
    ex11 = repo / "experiments/S007/20260915_S007_EX11"
    validate_experiment_archive(ex12)
    validate_experiment_archive(ex09)
    frozen = {
        ex12 / "experiment_manifest.json": sources["ex12_manifest_sha256"],
        ex12 / "artifacts/robustness_evidence.json": sources["robustness_evidence_sha256"],
        ex11 / "artifacts/temporal_diagnosis.json": sources["ex11_diagnosis_sha256"],
        ex09 / "artifacts/feasible_trials.csv": sources["feasible_trials_sha256"],
        ex09 / "run_experiment.py": sources["ex09_script_sha256"],
        repo / str(sources["feature_panel"]): sources["feature_panel_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen holding source differs: {path}")

    diagnosis = _read(ex11 / "artifacts/temporal_diagnosis.json")
    feasible = pd.read_csv(ex09 / "artifacts/feasible_trials.csv")
    matches = feasible.loc[feasible["trial_id"].eq(str(diagnosis["medoid_trial_id"]))]
    if len(matches) != 1:
        raise ValueError("EX11 medoid trial is not unique")
    metadata = json.loads(matches.iloc[0]["metadata"])
    helpers = _load_helpers(ex09 / "run_experiment.py")
    search_protocol = _read(ex09 / "artifacts/protocol.json")
    ex08 = repo / str(search_protocol["sources"]["ex08_archive"])
    effective = _read(ex08 / "artifacts/effective_search_protocol.json")
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
    combined = combined.where(oriented.notna().all(axis=1))
    decision = helpers._hysteresis(combined, float(metadata["entry_threshold"]), float(metadata["exit_threshold"]))
    raw_target = decision.shift(1).fillna(0.0)
    aligned_target = _minimum_holding_target(raw_target, int(protocol["minimum_holding_sessions"]))

    rows: list[dict[str, object]] = []
    for fee in protocol["fee_rates_one_way"]:
        original = helpers._metrics(raw_target, prices, float(fee))
        aligned = helpers._metrics(aligned_target, prices, float(fee))
        rows.append({"fee_rate_one_way": float(fee), "variant": "ORIGINAL", **original})
        rows.append({"fee_rate_one_way": float(fee), "variant": "MINIMUM_HOLD_3", **aligned})
    results = pd.DataFrame(rows)
    results.to_csv(artifacts / "holding_alignment_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    stress_fee = max(map(float, protocol["fee_rates_one_way"]))
    stress = results.loc[
        results["fee_rate_one_way"].eq(stress_fee) & results["variant"].eq("MINIMUM_HOLD_3")
    ].iloc[0]
    gates = protocol["hard_gates"]
    passed = (
        float(stress["cagr"]) >= float(gates["minimum_cagr"])
        and abs(min(float(stress["maximum_drawdown"]), 0.0)) <= float(gates["maximum_drawdown_loss"])
        and float(stress["rolling_60_closed_trades_median"]) >= float(gates["minimum_rolling_60_closed_trades_median"])
        and float(stress["rolling_60_closed_trades_p10"]) >= float(gates["minimum_rolling_60_closed_trades_p10"])
    )
    decision_code = "PROCEED_TO_CANDIDATE_FORMATION_AND_SE_AUDIT" if passed else "REJECT_HOLDING_ALIGNMENT_REMEDY"
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": decision_code,
        "minimum_holding_sessions": int(protocol["minimum_holding_sessions"]),
        "stress_pass": bool(passed),
        "stress_metrics": {
            "cagr": float(stress["cagr"]),
            "maximum_drawdown": float(stress["maximum_drawdown"]),
            "closed_trades": int(stress["closed_trades"]),
            "rolling_60_closed_trades_median": float(stress["rolling_60_closed_trades_median"]),
            "rolling_60_closed_trades_p10": float(stress["rolling_60_closed_trades_p10"]),
        },
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "holding_alignment_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S007 EX13 执行\n\n"
        f"状态：`COMPLETE`。3日最短持有在15bp单边费率下年化{float(stress['cagr']):.2%}、"
        f"最大回撤{float(stress['maximum_drawdown']):.2%}、闭合交易{int(stress['closed_trades'])}笔、"
        f"滚动60日中位数/P10为{float(stress['rolling_60_closed_trades_median']):.0f}/"
        f"{float(stress['rolling_60_closed_trades_p10']):.0f}。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX13 结论\n\n"
        f"裁决：`{decision_code}`。固定3日最短持有"
        f"{'解决了' if passed else '没有解决'}EX12识别的成本压力问题。"
        "本轮不搜索其他持有天数；若失败，应回到机制或执行结构层面，而不是继续调持有期。\n",
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
