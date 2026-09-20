from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)


EXPERIMENT_ID = "20260920_S007_EX41"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_hash(path: Path, expected: str) -> None:
    actual = _sha256(path)
    if actual != expected:
        raise ValueError(f"source hash differs: {path}; expected={expected}; actual={actual}")


def _summary(frame: pd.DataFrame) -> dict[str, float | int | None]:
    if frame.empty:
        return {
            "trade_count": 0,
            "year_count": 0,
            "positive_mean_years": 0,
            "win_rate": None,
            "sustained_rate": None,
            "mean_net_return": None,
            "median_net_return": None,
            "median_holding_sessions": None,
            "median_holding_mfe": None,
            "median_holding_mae": None,
        }
    year_means = frame.groupby(frame["entry_date"].dt.year)["net_return"].mean()
    return {
        "trade_count": int(len(frame)),
        "year_count": int(len(year_means)),
        "positive_mean_years": int(year_means.gt(0.0).sum()),
        "win_rate": float(frame["net_return"].gt(0.0).mean()),
        "sustained_rate": float(frame["holding_sessions"].ge(4).mean()),
        "mean_net_return": float(frame["net_return"].mean()),
        "median_net_return": float(frame["net_return"].median()),
        "median_holding_sessions": float(frame["holding_sessions"].median()),
        "median_holding_mfe": float(frame["holding_max_favorable_close_return"].median()),
        "median_holding_mae": float(frame["holding_max_adverse_close_return"].median()),
    }


def _permutation_difference(
    left: np.ndarray,
    right: np.ndarray,
    *,
    trials: int,
    rng: np.random.Generator,
) -> tuple[float, float]:
    if len(left) < 5 or len(right) < 5:
        return float("nan"), float("nan")
    observed = float(left.mean() - right.mean())
    combined = np.concatenate([left, right])
    left_count = len(left)
    exceedances = 0
    for _ in range(trials):
        permuted = rng.permutation(combined)
        difference = float(permuted[:left_count].mean() - permuted[left_count:].mean())
        exceedances += abs(difference) >= abs(observed)
    return observed, float((exceedances + 1) / (trials + 1))


def _rank_auc(score: pd.Series, outcome: pd.Series) -> float | None:
    valid = score.notna() & outcome.notna()
    positive = score.loc[valid & outcome.astype(bool)].astype(float).to_numpy()
    negative = score.loc[valid & ~outcome.astype(bool)].astype(float).to_numpy()
    if not len(positive) or not len(negative):
        return None
    comparisons = positive[:, None] - negative[None, :]
    return float(
        (np.count_nonzero(comparisons > 0) + 0.5 * np.count_nonzero(comparisons == 0))
        / comparisons.size
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    protocol = _read_json(experiment / "02_protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment identity differs")
    forbidden = (
        "reads_forward_data",
        "simulates_new_policy",
        "searches_parameters",
        "creates_candidate",
        "changes_strategy_parameters",
        "mutates_pte",
    )
    if any(protocol.get(field) for field in forbidden):
        raise ValueError("EX41 must remain an action-overlap diagnostic")

    sources = protocol["sources"]
    path_archive = repo / str(sources["path_archive"])
    validate_experiment_archive(path_archive)
    _require_hash(path_archive / "experiment_manifest.json", str(sources["path_manifest_sha256"]))
    source_path = path_archive / "artifacts/trade_path_attribution.csv"
    _require_hash(source_path, str(sources["trade_path_attribution_sha256"]))
    version_path = repo / str(sources["strategy_version"])
    _require_hash(version_path, str(sources["strategy_version_sha256"]))

    trades = pd.read_csv(source_path, parse_dates=["entry_date", "exit_date"])
    version = _read_json(version_path)
    rule = version["strategy_payload"]["rule"]
    fee_rate = float(rule["execution"]["capital"]["fee_rate"])
    exit_threshold = float(rule["score"]["exit_threshold"])
    if len(trades) != 139:
        raise ValueError("EX41 requires the frozen 139 closed trades")

    trades["first_close_mark_to_market_net"] = (
        (1.0 + trades["close_return_s1"]) * (1.0 - fee_rate) / (1.0 + fee_rate) - 1.0
    )
    trades["price_risk"] = trades["first_close_mark_to_market_net"].le(0.0)
    trades["factor_score_s1"] = trades["entry_factor_score"] + trades["factor_change_s1"]
    trades["factor_exit"] = trades["factor_score_s1"].le(exit_threshold)
    trades["actual_exit_next_session"] = trades["holding_sessions"].eq(1)
    if not trades["factor_exit"].equals(trades["actual_exit_next_session"]):
        mismatch = trades.loc[
            trades["factor_exit"].ne(trades["actual_exit_next_session"]), "cycle_id"
        ].tolist()
        raise ValueError(f"factor exit differs from formal next-session action: {mismatch}")

    trades["overlap_group"] = np.select(
        [
            trades["price_risk"] & trades["factor_exit"],
            trades["price_risk"] & ~trades["factor_exit"],
            ~trades["price_risk"] & trades["factor_exit"],
        ],
        ["BOTH", "PRICE_RISK_ONLY", "FACTOR_EXIT_ONLY"],
        default="NEITHER",
    )
    price_count = int(trades["price_risk"].sum())
    factor_count = int(trades["factor_exit"].sum())
    intersection = int((trades["price_risk"] & trades["factor_exit"]).sum())
    union = int((trades["price_risk"] | trades["factor_exit"]).sum())
    overlap = {
        "trade_count": int(len(trades)),
        "price_risk_count": price_count,
        "factor_exit_count": factor_count,
        "intersection_count": intersection,
        "union_count": union,
        "jaccard": float(intersection / union) if union else None,
        "price_risk_already_covered_rate": float(intersection / price_count) if price_count else None,
        "factor_exit_with_price_risk_rate": float(intersection / factor_count) if factor_count else None,
        "price_risk_incremental_count": int(
            (trades["price_risk"] & ~trades["factor_exit"]).sum()
        ),
        "factor_exit_price_healthy_count": int(
            (~trades["price_risk"] & trades["factor_exit"]).sum()
        ),
        "factor_exit_contract_mismatches": 0,
    }

    group_rows = [
        {"overlap_group": name, **_summary(group)}
        for name, group in trades.groupby("overlap_group", sort=True)
    ]
    price_only = trades.loc[trades["overlap_group"].eq("PRICE_RISK_ONLY")]
    neither = trades.loc[trades["overlap_group"].eq("NEITHER")]
    effect, pvalue = _permutation_difference(
        price_only["net_return"].to_numpy(dtype=float),
        neither["net_return"].to_numpy(dtype=float),
        trials=int(protocol["permutation_trials"]),
        rng=np.random.default_rng(int(protocol["random_seed"])),
    )
    factor_hold = trades.loc[~trades["factor_exit"]].copy()
    factor_hold["profitable"] = factor_hold["net_return"].gt(0.0)
    incremental = {
        "comparison": "PRICE_RISK_ONLY_MINUS_NEITHER",
        "price_risk_only": _summary(price_only),
        "neither": _summary(neither),
        "mean_net_return_difference": effect,
        "permutation_pvalue": pvalue,
        "factor_hold_trade_count": int(len(factor_hold)),
        "first_close_mark_to_market_auc_final_profit": _rank_auc(
            factor_hold["first_close_mark_to_market_net"], factor_hold["profitable"]
        ),
    }

    artifacts = experiment / "artifacts"
    artifacts.mkdir(exist_ok=True)
    trades.to_csv(
        artifacts / "trade_action_overlap.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    pd.DataFrame(group_rows).to_csv(
        artifacts / "overlap_group_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    evidence = {
        "schema_version": 1,
        "status": "PASS",
        "experiment_id": EXPERIMENT_ID,
        "credential_id": protocol["credential_id"],
        "strategy_release": "S007-v1",
        "development_cutoff": protocol["development_cutoff"],
        "checkpoint": protocol["checkpoint"],
        "fee_rate_per_side": fee_rate,
        "exit_threshold": exit_threshold,
        "overlap": overlap,
        "overlap_group_summary": group_rows,
        "incremental_evidence": incremental,
        "source_archive_validated": str(sources["path_archive"]),
        "forward_data_read": False,
        "new_policy_simulated": False,
        "strategy_parameters_changed": False,
        "candidate_created": False,
        "pte_mutated": False,
    }
    (artifacts / "action_overlap_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "strategy_version": protocol["strategy_version"],
            "credential_id": protocol["credential_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": "ACTION_OVERLAP_AUDIT_COMPLETE",
            "strategy_parameters_changed": False,
            "candidate_created": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
