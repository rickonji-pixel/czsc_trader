from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S006_EX04"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _rolling_state(
    frame: pd.DataFrame,
    *,
    signal_id: str,
    quantile: float,
    lookback: int,
) -> pd.DataFrame:
    ordered = frame.sort_values("first_usable_date").copy()
    values = ordered["value_numeric"].astype(float)
    threshold = values.shift(1).rolling(lookback, min_periods=lookback).quantile(quantile)
    state = pd.Series("warmup", index=ordered.index, dtype="string")
    ready = threshold.notna() & values.notna()
    state.loc[ready & values.ge(threshold)] = "active"
    state.loc[ready & values.lt(threshold)] = "inactive"
    return pd.DataFrame({
        "signal_id": signal_id,
        "information_date": ordered["information_date"],
        "first_usable_date": ordered["first_usable_date"],
        "first_usable_clock": ordered["first_usable_clock"],
        "factor_value": values,
        "threshold": threshold,
        "state": state,
    })


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("reads_new_returns"):
        raise ValueError("evaluation lock must not read new returns")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("evaluation lock cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    applicability = repo / "experiments/S006" / str(sources["applicability_experiment_id"])
    factors = repo / "experiments/S005" / str(sources["project_factor_experiment_id"])
    validate_experiment_archive(applicability)
    validate_experiment_archive(factors)
    expected_hashes = {
        applicability / "experiment_manifest.json": sources["applicability_manifest_sha256"],
        factors / "experiment_manifest.json": sources["project_factor_manifest_sha256"],
        repo / str(sources["s001_v2_path"]): sources["s001_v2_sha256"],
        repo / str(sources["s003_v1_path"]): sources["s003_v1_sha256"],
        repo / str(sources["breadth_protocol_path"]): sources["breadth_protocol_sha256"],
    }
    for path, expected in expected_hashes.items():
        if _sha256(path) != str(expected):
            raise ValueError(f"frozen source differs: {path}")

    ledger = pd.read_csv(factors / "artifacts/factor_observation_ledger.csv.gz")
    selected = ledger.loc[ledger["factor_id"].isin({
        "F-PROJECT-ER60", "F-PROJECT-BREADTH-THRUST-5", "F-PROJECT-MONEYFLOW-BREADTH",
    })].copy()
    if selected["value_numeric"].isna().any():
        raise ValueError("project signal factor material contains missing numeric values")
    selected["first_usable_date"] = pd.to_datetime(selected["first_usable_date"])
    selected["information_date"] = pd.to_datetime(selected["information_date"])

    parameters = protocol["project_signal_parameters"]
    er = selected.loc[selected["factor_id"].eq("F-PROJECT-ER60")].sort_values("first_usable_date")
    er_threshold = float(parameters["SIG-PROJECT-ER60-REGIME"]["threshold"])
    er_state = pd.Series("range", index=er.index, dtype="string")
    er_state.loc[er["value_numeric"].astype(float).ge(er_threshold)] = "trend"
    er_signal = pd.DataFrame({
        "signal_id": "SIG-PROJECT-ER60-REGIME",
        "information_date": er["information_date"],
        "first_usable_date": er["first_usable_date"],
        "first_usable_clock": er["first_usable_clock"],
        "factor_value": er["value_numeric"].astype(float),
        "threshold": er_threshold,
        "state": er_state,
    })
    breadth = _rolling_state(
        selected.loc[selected["factor_id"].eq("F-PROJECT-BREADTH-THRUST-5")],
        signal_id="SIG-PROJECT-BREADTH-THRUST",
        quantile=float(parameters["SIG-PROJECT-BREADTH-THRUST"]["threshold_quantile"]),
        lookback=int(parameters["SIG-PROJECT-BREADTH-THRUST"]["lookback_sessions"]),
    )
    moneyflow = _rolling_state(
        selected.loc[selected["factor_id"].eq("F-PROJECT-MONEYFLOW-BREADTH")],
        signal_id="SIG-PROJECT-MONEYFLOW-BREADTH",
        quantile=float(parameters["SIG-PROJECT-MONEYFLOW-BREADTH"]["threshold_quantile"]),
        lookback=int(parameters["SIG-PROJECT-MONEYFLOW-BREADTH"]["lookback_sessions"]),
    )
    signal_states = pd.concat([er_signal, breadth, moneyflow], ignore_index=True)
    signal_states = signal_states.sort_values(["signal_id", "first_usable_date"]).reset_index(drop=True)
    signal_states.to_csv(artifacts / "project_signal_states.csv.gz", index=False, compression="gzip")

    state_counts = (
        signal_states.groupby(["signal_id", "state"], observed=True).size().rename("observations").reset_index()
    )
    state_counts.to_csv(artifacts / "project_signal_state_counts.csv", index=False)
    evaluation = protocol["evaluation"]
    expected_paths = (
        int(evaluation["categorical_configurations"])
        + int(evaluation["continuous_factors"])
        + int(evaluation["event_factors"])
    ) * len(evaluation["forward_open_intervals"])
    if expected_paths != int(evaluation["preregistered_return_paths"]):
        raise ValueError("pre-registered return path count is inconsistent")
    _write(
        artifacts / "materialization_evidence.json",
        {
            "schema_version": 1,
            "experiment_id": EXPERIMENT_ID,
            "project_signal_count": int(signal_states["signal_id"].nunique()),
            "state_observation_count": int(len(signal_states)),
            "signal_state_counts": state_counts.to_dict(orient="records"),
            "categorical_configurations": int(evaluation["categorical_configurations"]),
            "continuous_factors": int(evaluation["continuous_factors"]),
            "event_factors": int(evaluation["event_factors"]),
            "forward_open_intervals": evaluation["forward_open_intervals"],
            "preregistered_return_paths": expected_paths,
            "new_return_paths_read": 0,
            "candidate_created": False,
            "strategy_manager_mutated": False,
            "pte_mutated": False,
        },
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
            "decision": "PROCEED_TO_PREREGISTERED_FULL_INFORMATION_AUDIT",
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
