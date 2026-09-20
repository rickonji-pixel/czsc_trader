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


EXPERIMENT_ID = "20260920_S007_EX40"


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


def _permutation_difference(
    values: np.ndarray,
    winners: np.ndarray,
    *,
    trials: int,
    rng: np.random.Generator,
) -> tuple[float, float]:
    winner_values = values[winners]
    loser_values = values[~winners]
    if len(winner_values) < 5 or len(loser_values) < 5:
        return float("nan"), float("nan")
    observed = float(winner_values.mean() - loser_values.mean())
    winner_count = int(winners.sum())
    exceedances = 0
    for _ in range(trials):
        permuted = rng.permutation(values)
        difference = float(permuted[:winner_count].mean() - permuted[winner_count:].mean())
        exceedances += abs(difference) >= abs(observed)
    return observed, float((exceedances + 1) / (trials + 1))


def _bh_qvalues(pvalues: pd.Series) -> pd.Series:
    result = pd.Series(np.nan, index=pvalues.index, dtype=float)
    valid = pvalues.dropna().sort_values()
    if valid.empty:
        return result
    count = len(valid)
    adjusted = valid.to_numpy(dtype=float) * count / np.arange(1, count + 1)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    result.loc[valid.index] = np.minimum(adjusted, 1.0)
    return result


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


def _mean_or_none(values: pd.Series) -> float | None:
    valid = values.dropna().astype(float)
    return float(valid.mean()) if len(valid) else None


def _median_or_none(values: pd.Series) -> float | None:
    valid = values.dropna().astype(float)
    return float(valid.median()) if len(valid) else None


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    protocol = _read_json(experiment / "02_protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment identity differs")
    forbidden = (
        "reads_forward_data",
        "searches_parameters",
        "creates_candidate",
        "changes_strategy_parameters",
        "mutates_pte",
    )
    if any(protocol.get(field) for field in forbidden):
        raise ValueError("EX40 must remain a frozen-strategy diagnostic")

    sources = protocol["sources"]
    replay = repo / str(sources["formal_replay_archive"])
    attribution = repo / str(sources["attribution_archive"])
    validate_experiment_archive(replay)
    validate_experiment_archive(attribution)
    _require_hash(replay / "experiment_manifest.json", str(sources["formal_replay_manifest_sha256"]))
    _require_hash(attribution / "experiment_manifest.json", str(sources["attribution_manifest_sha256"]))
    replay_artifacts = replay / "artifacts"
    for filename, hash_field in (
        ("trades.csv", "trades_sha256"),
        ("decisions.csv", "decisions_sha256"),
        ("account_daily.csv", "account_daily_sha256"),
        ("fills.csv", "fills_sha256"),
    ):
        _require_hash(replay_artifacts / filename, str(sources[hash_field]))
    attribution_path = attribution / "artifacts/trade_attribution.csv"
    _require_hash(attribution_path, str(sources["trade_attribution_sha256"]))
    version_path = repo / str(sources["strategy_version"])
    _require_hash(version_path, str(sources["strategy_version_sha256"]))

    raw_trades = pd.read_csv(replay_artifacts / "trades.csv")
    ledger = pd.read_csv(
        attribution_path,
        parse_dates=["entry_signal_date", "exit_signal_date", "entry_date", "exit_date"],
    )
    decisions = pd.read_csv(replay_artifacts / "decisions.csv", parse_dates=["signal_date"])
    daily = pd.read_csv(replay_artifacts / "account_daily.csv", parse_dates=["date"])
    version = _read_json(version_path)
    score = version["strategy_payload"]["rule"]["score"]
    confirmation_threshold = float(score["confirmation_threshold"])
    exit_threshold = float(score["exit_threshold"])

    if len(raw_trades) != 139 or len(ledger) != 139:
        raise ValueError("EX40 requires the frozen 139 closed trades")
    expected = raw_trades.set_index("cycle_id")["net_return"].sort_index()
    observed = ledger.set_index("cycle_id")["net_return"].sort_index()
    if not expected.index.equals(observed.index) or not np.array_equal(
        expected.to_numpy(dtype=float), observed.to_numpy(dtype=float)
    ):
        raise ValueError("EX39 trade attribution differs from the formal replay")

    decision_scores = decisions.set_index("signal_date")[["factor_score", "confirmation_score"]].sort_index()
    if decision_scores.index.duplicated().any():
        raise ValueError("decision signal dates must be unique")
    close = daily.drop_duplicates("date", keep="last").set_index("date")["close"].astype(float).sort_index()
    sessions = pd.DatetimeIndex(close.index)
    session_position = pd.Series(np.arange(len(sessions)), index=sessions)
    early_sessions = int(protocol["early_path_sessions"])
    horizons = [int(value) for value in protocol["post_exit_close_horizons"]]

    trade_rows: list[dict[str, Any]] = []
    path_rows: list[dict[str, Any]] = []
    for trade in ledger.itertuples(index=False):
        if trade.entry_signal_date not in decision_scores.index or trade.exit_signal_date not in decision_scores.index:
            raise ValueError(f"missing decision score for cycle: {trade.cycle_id}")
        holding_dates = sessions[(sessions >= trade.entry_date) & (sessions < trade.exit_date)]
        if len(holding_dates) != int(trade.holding_sessions):
            raise ValueError(f"holding calendar differs for cycle: {trade.cycle_id}")
        if not len(holding_dates) or holding_dates[-1] != trade.exit_signal_date:
            raise ValueError(f"exit signal is not the final holding-session close: {trade.cycle_id}")

        entry_scores = decision_scores.loc[trade.entry_signal_date]
        path_rows.append(
            {
                "cycle_id": trade.cycle_id,
                "outcome_group": trade.outcome_group,
                "age": 0,
                "date": trade.entry_signal_date,
                "factor_score": float(entry_scores["factor_score"]),
                "confirmation_score": float(entry_scores["confirmation_score"]),
                "factor_exit_margin": float(entry_scores["factor_score"] - exit_threshold),
                "confirmation_entry_margin": float(
                    entry_scores["confirmation_score"] - confirmation_threshold
                ),
                "close_return_from_entry": np.nan,
            }
        )
        holding_path: list[dict[str, float]] = []
        for age, date in enumerate(holding_dates, start=1):
            scores = decision_scores.loc[date]
            close_return = float(close.loc[date] / float(trade.entry_price) - 1.0)
            row = {
                "cycle_id": trade.cycle_id,
                "outcome_group": trade.outcome_group,
                "age": age,
                "date": date,
                "factor_score": float(scores["factor_score"]),
                "confirmation_score": float(scores["confirmation_score"]),
                "factor_exit_margin": float(scores["factor_score"] - exit_threshold),
                "confirmation_entry_margin": float(scores["confirmation_score"] - confirmation_threshold),
                "close_return_from_entry": close_return,
            }
            path_rows.append(row)
            holding_path.append(row)

        first = holding_path[0]
        early = holding_path[:early_sessions]
        close_returns = pd.Series([item["close_return_from_entry"] for item in holding_path])
        gross_return = float(float(trade.exit_price) / float(trade.entry_price) - 1.0)
        result: dict[str, Any] = {
            "cycle_id": trade.cycle_id,
            "outcome_group": trade.outcome_group,
            "entry_date": trade.entry_date,
            "exit_date": trade.exit_date,
            "holding_sessions": int(trade.holding_sessions),
            "net_return": float(trade.net_return),
            "gross_return": gross_return,
            "entry_factor_score": float(trade.entry_factor_score),
            "entry_confirmation_score": float(trade.entry_confirmation_score),
            "exit_factor_score": float(trade.exit_factor_score),
            "close_return_s1": first["close_return_from_entry"],
            "factor_change_s1": first["factor_score"] - float(trade.entry_factor_score),
            "confirmation_change_s1": (
                first["confirmation_score"] - float(trade.entry_confirmation_score)
            ),
            "factor_min_exit_margin_first2": min(item["factor_exit_margin"] for item in early),
            "confirmation_min_entry_margin_first2": min(
                item["confirmation_entry_margin"] for item in early
            ),
            "max_adverse_close_return_first2": min(
                item["close_return_from_entry"] for item in early
            ),
            "max_favorable_close_return_first2": max(
                item["close_return_from_entry"] for item in early
            ),
            "holding_max_favorable_close_return": float(close_returns.max()),
            "holding_max_adverse_close_return": float(close_returns.min()),
            "gave_back_from_close_peak": float(close_returns.max() - gross_return),
            "exit_factor_gap": float(exit_threshold - float(trade.exit_factor_score)),
            "factor_score_decay": float(trade.entry_factor_score - trade.exit_factor_score),
            "confirmation_score_decay": float(
                trade.entry_confirmation_score - trade.exit_confirmation_score
            ),
        }
        exit_offset = int(session_position.loc[trade.exit_date])
        for horizon in horizons:
            future_offset = exit_offset + horizon - 1
            result[f"post_exit_close_return_{horizon}"] = (
                float(close.iloc[future_offset] / float(trade.exit_price) - 1.0)
                if future_offset < len(close)
                else np.nan
            )
        trade_rows.append(result)

    paths = pd.DataFrame(path_rows)
    path_trades = pd.DataFrame(trade_rows)
    if len(path_trades) != 139:
        raise ValueError("path attribution did not preserve all trades")

    early_features = [
        "close_return_s1",
        "factor_change_s1",
        "confirmation_change_s1",
        "factor_min_exit_margin_first2",
        "confirmation_min_entry_margin_first2",
        "max_adverse_close_return_first2",
    ]
    short = path_trades.loc[
        path_trades["outcome_group"].isin(["SHORT_LOSS", "SHORT_WIN"])
    ].copy()
    short["short_win"] = short["outcome_group"].eq("SHORT_WIN")
    rng = np.random.default_rng(int(protocol["random_seed"]))
    comparison_rows: list[dict[str, Any]] = []
    for feature in early_features:
        valid = short[feature].notna()
        values = short.loc[valid, feature].to_numpy(dtype=float)
        winners = short.loc[valid, "short_win"].to_numpy(dtype=bool)
        effect, pvalue = _permutation_difference(
            values,
            winners,
            trials=int(protocol["permutation_trials"]),
            rng=rng,
        )
        comparison_rows.append(
            {
                "feature": feature,
                "short_loss_count": int((~winners).sum()),
                "short_win_count": int(winners.sum()),
                "short_loss_mean": float(values[~winners].mean()),
                "short_win_mean": float(values[winners].mean()),
                "mean_difference_win_minus_loss": effect,
                "short_win_auc": _rank_auc(short.loc[valid, feature], short.loc[valid, "short_win"]),
                "permutation_pvalue": pvalue,
            }
        )
    early_comparisons = pd.DataFrame(comparison_rows)
    early_comparisons["bh_fdr_qvalue"] = _bh_qvalues(early_comparisons["permutation_pvalue"])

    group_rows: list[dict[str, Any]] = []
    for name, group in path_trades.groupby("outcome_group", sort=True):
        group_rows.append(
            {
                "outcome_group": name,
                "trade_count": int(len(group)),
                "mean_holding_sessions": float(group["holding_sessions"].mean()),
                "mean_net_return": float(group["net_return"].mean()),
                "median_close_return_s1": float(group["close_return_s1"].median()),
                "median_factor_change_s1": float(group["factor_change_s1"].median()),
                "median_confirmation_change_s1": float(group["confirmation_change_s1"].median()),
                "median_holding_mfe": float(group["holding_max_favorable_close_return"].median()),
                "median_holding_mae": float(group["holding_max_adverse_close_return"].median()),
                "median_giveback_from_close_peak": float(group["gave_back_from_close_peak"].median()),
                "median_exit_factor_gap": float(group["exit_factor_gap"].median()),
                "median_factor_score_decay": float(group["factor_score_decay"].median()),
                "median_confirmation_score_decay": float(group["confirmation_score_decay"].median()),
            }
        )

    age_summary = (
        paths.loc[paths["age"].le(5)]
        .groupby(["outcome_group", "age"], sort=True)
        .agg(
            trade_count=("cycle_id", "count"),
            median_factor_exit_margin=("factor_exit_margin", "median"),
            median_confirmation_entry_margin=("confirmation_entry_margin", "median"),
            median_close_return_from_entry=("close_return_from_entry", "median"),
        )
        .reset_index()
    )

    post_exit_rows: list[dict[str, Any]] = []
    for group_name, group in [("ALL", path_trades), *path_trades.groupby("outcome_group", sort=True)]:
        for horizon in horizons:
            column = f"post_exit_close_return_{horizon}"
            valid = group.loc[group[column].notna()].copy()
            year_means = valid.groupby(valid["exit_date"].dt.year)[column].mean()
            post_exit_rows.append(
                {
                    "outcome_group": group_name,
                    "horizon_sessions_including_exit_day": horizon,
                    "trade_count": int(len(valid)),
                    "mean_close_return": _mean_or_none(valid[column]),
                    "median_close_return": _median_or_none(valid[column]),
                    "positive_rate": float(valid[column].gt(0.0).mean()) if len(valid) else None,
                    "positive_mean_years": int(year_means.gt(0.0).sum()),
                    "year_count": int(len(year_means)),
                }
            )
    post_exit = pd.DataFrame(post_exit_rows)

    artifacts = experiment / "artifacts"
    artifacts.mkdir(exist_ok=True)
    path_trades.to_csv(
        artifacts / "trade_path_attribution.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    paths.to_csv(
        artifacts / "aligned_holding_paths.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    early_comparisons.to_csv(
        artifacts / "early_path_comparisons.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    pd.DataFrame(group_rows).to_csv(
        artifacts / "outcome_path_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    age_summary.to_csv(
        artifacts / "aligned_age_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    post_exit.to_csv(
        artifacts / "post_exit_close_diagnostic.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    evidence = {
        "schema_version": 1,
        "status": "PASS",
        "experiment_id": EXPERIMENT_ID,
        "credential_id": protocol["credential_id"],
        "strategy_release": "S007-v1",
        "development_cutoff": protocol["development_cutoff"],
        "trade_count": int(len(path_trades)),
        "early_path_comparisons": early_comparisons.replace({np.nan: None}).to_dict(orient="records"),
        "outcome_path_summary": group_rows,
        "post_exit_close_diagnostic": post_exit.replace({np.nan: None}).to_dict(orient="records"),
        "source_archives_validated": [
            str(sources["formal_replay_archive"]),
            str(sources["attribution_archive"]),
        ],
        "forward_data_read": False,
        "strategy_parameters_changed": False,
        "candidate_created": False,
        "pte_mutated": False,
    }
    (artifacts / "path_attribution_evidence.json").write_text(
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
            "decision": "PATH_ATTRIBUTION_COMPLETE",
            "strategy_parameters_changed": False,
            "candidate_created": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
