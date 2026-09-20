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


EXPERIMENT_ID = "20260920_S007_EX39"


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


def _compound(values: pd.Series) -> float:
    return float(np.prod(1.0 + values.astype(float).to_numpy()) - 1.0)


def _summary(frame: pd.DataFrame) -> dict[str, float | int | None]:
    returns = frame["net_return"].astype(float)
    positive = returns.clip(lower=0.0)
    negative = returns.clip(upper=0.0).abs()
    negative_total = float(negative.sum())
    return {
        "trade_count": int(len(frame)),
        "year_count": int(frame["entry_date"].dt.year.nunique()),
        "win_rate": float(returns.gt(0.0).mean()) if len(frame) else None,
        "sustained_rate": float(frame["sustained_trade"].mean()) if len(frame) else None,
        "short_loss_rate": float(frame["short_loss"].mean()) if len(frame) else None,
        "mean_net_return": float(returns.mean()) if len(frame) else None,
        "median_net_return": float(returns.median()) if len(frame) else None,
        "compound_trade_return": _compound(returns) if len(frame) else None,
        "positive_return_sum": float(positive.sum()),
        "negative_return_abs_sum": negative_total,
        "profit_factor": float(positive.sum() / negative_total) if negative_total > 0 else None,
    }


def _permutation_mean_difference(
    values: np.ndarray,
    groups: np.ndarray,
    *,
    trials: int,
    rng: np.random.Generator,
) -> tuple[float, float]:
    true_values = values[groups]
    false_values = values[~groups]
    if len(true_values) < 5 or len(false_values) < 5:
        return float("nan"), float("nan")
    observed = float(true_values.mean() - false_values.mean())
    true_count = int(groups.sum())
    exceedances = 0
    for _ in range(trials):
        permuted = rng.permutation(values)
        difference = float(permuted[:true_count].mean() - permuted[true_count:].mean())
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
    values = score.loc[valid].astype(float)
    labels = outcome.loc[valid].astype(bool)
    positive = values.loc[labels].to_numpy()
    negative = values.loc[~labels].to_numpy()
    if not len(positive) or not len(negative):
        return None
    comparisons = positive[:, None] - negative[None, :]
    return float((np.count_nonzero(comparisons > 0) + 0.5 * np.count_nonzero(comparisons == 0)) / comparisons.size)


def _rank_correlation(left: pd.Series, right: pd.Series) -> float | None:
    valid = left.notna() & right.notna()
    if valid.sum() < 5:
        return None
    return float(left.loc[valid].rank().corr(right.loc[valid].rank()))


def _causal_entry_strength(values: pd.Series, minimum: int) -> pd.Series:
    threshold = values.shift(1).expanding(min_periods=minimum).median()
    result = values.ge(threshold).astype("boolean")
    return result.mask(threshold.isna())


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
        raise ValueError("EX39 must remain a frozen-strategy diagnostic")

    sources = protocol["sources"]
    replay = repo / str(sources["formal_replay_archive"])
    features_archive = repo / str(sources["feature_archive"])
    validate_experiment_archive(replay)
    validate_experiment_archive(features_archive)
    _require_hash(replay / "experiment_manifest.json", str(sources["formal_replay_manifest_sha256"]))
    _require_hash(features_archive / "experiment_manifest.json", str(sources["feature_archive_manifest_sha256"]))
    replay_artifacts = replay / "artifacts"
    for filename, hash_field in (
        ("trades.csv", "trades_sha256"),
        ("decisions.csv", "decisions_sha256"),
        ("account_daily.csv", "account_daily_sha256"),
        ("fills.csv", "fills_sha256"),
    ):
        _require_hash(replay_artifacts / filename, str(sources[hash_field]))
    feature_path = features_archive / "artifacts/causal_feature_panel.csv.gz"
    _require_hash(feature_path, str(sources["feature_panel_sha256"]))
    version_path = repo / str(sources["strategy_version"])
    _require_hash(version_path, str(sources["strategy_version_sha256"]))

    trades = pd.read_csv(replay_artifacts / "trades.csv", parse_dates=["entry_date", "exit_date"])
    decisions = pd.read_csv(replay_artifacts / "decisions.csv", parse_dates=["signal_date", "valid_session"])
    fills = pd.read_csv(replay_artifacts / "fills.csv", parse_dates=["signal_date", "fill_time"])
    daily = pd.read_csv(replay_artifacts / "account_daily.csv", parse_dates=["date", "signal_date"])
    features = pd.read_csv(feature_path, parse_dates=["date"]).set_index("date").sort_index()
    version = _read_json(version_path)
    score = version["strategy_payload"]["rule"]["score"]

    if len(trades) != 139 or not trades["status"].eq("CLOSED").all():
        raise ValueError("formal replay must contain the frozen 139 closed trades")
    if trades["cycle_id"].duplicated().any():
        raise ValueError("cycle ids must be unique")

    buy = fills.loc[fills["side"].eq("BUY"), ["cycle_id", "signal_date"]].rename(
        columns={"signal_date": "entry_signal_date"}
    )
    sell = fills.loc[fills["side"].eq("SELL"), ["cycle_id", "signal_date"]].rename(
        columns={"signal_date": "exit_signal_date"}
    )
    if buy["cycle_id"].duplicated().any() or sell["cycle_id"].duplicated().any():
        raise ValueError("each closed cycle must have one buy and one sell fill")
    ledger = trades.merge(buy, on="cycle_id", how="left", validate="one_to_one")
    ledger = ledger.merge(sell, on="cycle_id", how="left", validate="one_to_one")

    decision_scores = decisions.set_index("signal_date")[["factor_score", "confirmation_score"]]
    if decision_scores.index.duplicated().any():
        raise ValueError("decision signal dates must be unique")
    entry_scores = decision_scores.rename(
        columns={"factor_score": "entry_factor_score", "confirmation_score": "entry_confirmation_score"}
    )
    exit_scores = decision_scores.rename(
        columns={"factor_score": "exit_factor_score", "confirmation_score": "exit_confirmation_score"}
    )
    ledger = ledger.join(entry_scores, on="entry_signal_date")
    ledger = ledger.join(exit_scores, on="exit_signal_date")
    required_scores = ["entry_factor_score", "entry_confirmation_score", "exit_factor_score"]
    if ledger[required_scores].isna().any().any():
        raise ValueError("formal trade cycles must resolve to entry and exit decision scores")

    session_positions = pd.Series(np.arange(len(features)), index=features.index)
    if not ledger["entry_date"].isin(session_positions.index).all() or not ledger["exit_date"].isin(session_positions.index).all():
        raise ValueError("trade dates must exist in the frozen feature calendar")
    ledger["holding_sessions"] = (
        ledger["exit_date"].map(session_positions) - ledger["entry_date"].map(session_positions)
    ).astype(int)
    short_max = int(protocol["short_holding_max_sessions"])
    ledger["short_trade"] = ledger["holding_sessions"].le(short_max)
    ledger["sustained_trade"] = ~ledger["short_trade"]
    ledger["short_loss"] = ledger["short_trade"] & ledger["net_return"].le(0.0)
    ledger["profitable_trade"] = ledger["net_return"].gt(0.0)
    ledger["entry_factor_margin"] = ledger["entry_factor_score"] - float(score["entry_threshold"])
    ledger["entry_confirmation_margin"] = (
        ledger["entry_confirmation_score"] - float(score["confirmation_threshold"])
    )
    ledger["exit_factor_gap"] = float(score["exit_threshold"]) - ledger["exit_factor_score"]
    ledger["factor_score_decay"] = ledger["entry_factor_score"] - ledger["exit_factor_score"]

    close = daily.drop_duplicates("date", keep="last").set_index("date")["close"].astype(float).sort_index()
    trend_window = int(protocol["trend_window_sessions"])
    drawdown_window = int(protocol["drawdown_window_sessions"])
    trend_ma = close.rolling(trend_window, min_periods=trend_window).mean()
    drawdown = close / close.rolling(drawdown_window, min_periods=drawdown_window).max() - 1.0
    entry_dates = ledger["entry_signal_date"]
    ledger["entry_close"] = entry_dates.map(close)
    ledger["trend_ma_60"] = entry_dates.map(trend_ma)
    ledger["drawdown_126"] = entry_dates.map(drawdown)
    ledger["trend_up_60"] = ledger["entry_close"].ge(ledger["trend_ma_60"]).astype("boolean").mask(
        ledger["trend_ma_60"].isna()
    )
    ledger["deep_drawdown_126"] = ledger["drawdown_126"].le(
        float(protocol["deep_drawdown_threshold"])
    ).astype("boolean").mask(ledger["drawdown_126"].isna())

    volatility = features["volatility_realized_20"].astype(float)
    volatility_threshold = volatility.shift(1).rolling(
        int(protocol["volatility_history_sessions"]),
        min_periods=int(protocol["volatility_minimum_sessions"]),
    ).median()
    ledger["volatility_realized_20"] = entry_dates.map(volatility)
    ledger["volatility_history_median"] = entry_dates.map(volatility_threshold)
    ledger["high_volatility_20"] = ledger["volatility_realized_20"].gt(
        ledger["volatility_history_median"]
    ).astype("boolean").mask(ledger["volatility_history_median"].isna())
    ledger["liquidity_volume_ratio_20"] = entry_dates.map(features["liquidity_volume_ratio_20"])
    ledger["active_liquidity_20"] = ledger["liquidity_volume_ratio_20"].ge(1.0).astype("boolean").mask(
        ledger["liquidity_volume_ratio_20"].isna()
    )
    ledger["risk_chinext_turnover_z20"] = entry_dates.map(features["risk_chinext_turnover_z20"])
    ledger["risk_appetite_positive"] = ledger["risk_chinext_turnover_z20"].gt(0.0).astype("boolean").mask(
        ledger["risk_chinext_turnover_z20"].isna()
    )

    score_minimum = int(protocol["score_history_minimum_trades"])
    ledger = ledger.sort_values("entry_date").reset_index(drop=True)
    ledger["strong_factor_entry"] = _causal_entry_strength(ledger["entry_factor_margin"], score_minimum)
    ledger["strong_confirmation_entry"] = _causal_entry_strength(
        ledger["entry_confirmation_margin"], score_minimum
    )

    dimensions = [
        "trend_up_60",
        "deep_drawdown_126",
        "high_volatility_20",
        "active_liquidity_20",
        "risk_appetite_positive",
        "strong_factor_entry",
        "strong_confirmation_entry",
    ]
    summary_rows: list[dict[str, Any]] = []
    contrast_rows: list[dict[str, Any]] = []
    rng = np.random.default_rng(int(protocol["random_seed"]))
    for dimension in dimensions:
        valid = ledger.loc[ledger[dimension].notna()].copy()
        for state in (False, True):
            group = valid.loc[valid[dimension].astype(bool).eq(state)]
            summary_rows.append({"dimension": dimension, "state": state, **_summary(group)})
        values = valid["net_return"].to_numpy(dtype=float)
        groups = valid[dimension].astype(bool).to_numpy()
        effect, pvalue = _permutation_mean_difference(
            values,
            groups,
            trials=int(protocol["permutation_trials"]),
            rng=rng,
        )
        false_group = valid.loc[~valid[dimension].astype(bool)]
        true_group = valid.loc[valid[dimension].astype(bool)]
        contrast_rows.append(
            {
                "dimension": dimension,
                "false_count": len(false_group),
                "true_count": len(true_group),
                "mean_return_difference_true_minus_false": effect,
                "sustained_rate_difference_true_minus_false": (
                    float(true_group["sustained_trade"].mean() - false_group["sustained_trade"].mean())
                    if len(false_group) and len(true_group)
                    else np.nan
                ),
                "short_loss_rate_difference_true_minus_false": (
                    float(true_group["short_loss"].mean() - false_group["short_loss"].mean())
                    if len(false_group) and len(true_group)
                    else np.nan
                ),
                "permutation_pvalue": pvalue,
            }
        )
    regime_summary = pd.DataFrame(summary_rows)
    contrasts = pd.DataFrame(contrast_rows)
    contrasts["bh_fdr_qvalue"] = _bh_qvalues(contrasts["permutation_pvalue"])

    composite = ledger.loc[ledger["trend_up_60"].notna() & ledger["high_volatility_20"].notna()].copy()
    composite["trend_volatility_state"] = np.where(
        composite["trend_up_60"].astype(bool), "UP", "DOWN"
    ) + "_" + np.where(composite["high_volatility_20"].astype(bool), "HIGH_VOL", "LOW_VOL")
    composite_rows = [
        {"trend_volatility_state": state, **_summary(group)}
        for state, group in composite.groupby("trend_volatility_state", sort=True)
    ]

    outcome_group = np.select(
        [
            ledger["short_trade"] & ledger["profitable_trade"],
            ledger["short_trade"] & ~ledger["profitable_trade"],
            ledger["sustained_trade"] & ledger["profitable_trade"],
        ],
        ["SHORT_WIN", "SHORT_LOSS", "SUSTAINED_WIN"],
        default="SUSTAINED_LOSS",
    )
    ledger["outcome_group"] = outcome_group
    outcome_rows: list[dict[str, Any]] = []
    for name, group in ledger.groupby("outcome_group", sort=True):
        outcome_rows.append(
            {
                "outcome_group": name,
                **_summary(group),
                "mean_holding_sessions": float(group["holding_sessions"].mean()),
                "median_entry_factor_margin": float(group["entry_factor_margin"].median()),
                "median_entry_confirmation_margin": float(group["entry_confirmation_margin"].median()),
                "median_exit_factor_gap": float(group["exit_factor_gap"].median()),
                "median_factor_score_decay": float(group["factor_score_decay"].median()),
            }
        )

    year_rows = [
        {"entry_year": int(year), **_summary(group)}
        for year, group in ledger.groupby(ledger["entry_date"].dt.year, sort=True)
    ]
    positive = ledger.loc[ledger["net_return"].gt(0.0), "net_return"].sort_values(ascending=False)
    negative = ledger.loc[ledger["net_return"].lt(0.0), "net_return"].abs().sort_values(ascending=False)
    positive_total = float(positive.sum())
    negative_total = float(negative.sum())
    positive_hhi = float(np.square(positive / positive_total).sum()) if positive_total else None
    negative_hhi = float(np.square(negative / negative_total).sum()) if negative_total else None
    rolling = ledger["net_return"].rolling(int(protocol["rolling_trade_window"]), min_periods=int(protocol["rolling_trade_window"])).mean().dropna()
    score_diagnostics = {
        "entry_factor_margin_auc_sustained": _rank_auc(
            ledger["entry_factor_margin"], ledger["sustained_trade"]
        ),
        "entry_factor_margin_auc_profitable": _rank_auc(
            ledger["entry_factor_margin"], ledger["profitable_trade"]
        ),
        "entry_confirmation_margin_auc_sustained": _rank_auc(
            ledger["entry_confirmation_margin"], ledger["sustained_trade"]
        ),
        "entry_confirmation_margin_auc_profitable": _rank_auc(
            ledger["entry_confirmation_margin"], ledger["profitable_trade"]
        ),
        "entry_factor_margin_rank_corr_holding": _rank_correlation(
            ledger["entry_factor_margin"], ledger["holding_sessions"]
        ),
        "entry_factor_margin_rank_corr_return": _rank_correlation(
            ledger["entry_factor_margin"], ledger["net_return"]
        ),
        "entry_confirmation_margin_rank_corr_holding": _rank_correlation(
            ledger["entry_confirmation_margin"], ledger["holding_sessions"]
        ),
        "entry_confirmation_margin_rank_corr_return": _rank_correlation(
            ledger["entry_confirmation_margin"], ledger["net_return"]
        ),
    }
    concentration = {
        "profitable_trades": int(len(positive)),
        "losing_trades": int(len(negative)),
        "top_1_positive_share": float(positive.head(1).sum() / positive_total),
        "top_3_positive_share": float(positive.head(3).sum() / positive_total),
        "top_5_positive_share": float(positive.head(5).sum() / positive_total),
        "positive_return_hhi": positive_hhi,
        "effective_profitable_trade_count": float(1.0 / positive_hhi) if positive_hhi else None,
        "top_1_negative_share": float(negative.head(1).sum() / negative_total),
        "top_3_negative_share": float(negative.head(3).sum() / negative_total),
        "top_5_negative_share": float(negative.head(5).sum() / negative_total),
        "negative_return_hhi": negative_hhi,
        "effective_losing_trade_count": float(1.0 / negative_hhi) if negative_hhi else None,
    }
    rolling_summary = {
        "window_trades": int(protocol["rolling_trade_window"]),
        "window_count": int(len(rolling)),
        "positive_mean_window_rate": float(rolling.gt(0.0).mean()),
        "minimum_mean_return": float(rolling.min()),
        "median_mean_return": float(rolling.median()),
        "maximum_mean_return": float(rolling.max()),
    }

    artifacts = experiment / "artifacts"
    artifacts.mkdir(exist_ok=True)
    ledger.to_csv(artifacts / "trade_attribution.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    regime_summary.to_csv(artifacts / "regime_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    contrasts.to_csv(artifacts / "regime_contrasts.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    pd.DataFrame(composite_rows).to_csv(
        artifacts / "composite_regime_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    pd.DataFrame(outcome_rows).to_csv(
        artifacts / "outcome_group_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    pd.DataFrame(year_rows).to_csv(
        artifacts / "yearly_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    top_trades = ledger.nlargest(5, "net_return")[
        [
            "cycle_id",
            "entry_signal_date",
            "entry_date",
            "exit_date",
            "holding_sessions",
            "net_return",
            *dimensions,
        ]
    ]
    top_trades.to_csv(
        artifacts / "top_profitable_trades.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    evidence = {
        "schema_version": 1,
        "status": "PASS",
        "experiment_id": EXPERIMENT_ID,
        "credential_id": protocol["credential_id"],
        "strategy_release": "S007-v1",
        "development_cutoff": protocol["development_cutoff"],
        "trade_count": int(len(ledger)),
        "short_trade_count": int(ledger["short_trade"].sum()),
        "short_loss_count": int(ledger["short_loss"].sum()),
        "sustained_trade_count": int(ledger["sustained_trade"].sum()),
        "score_diagnostics": score_diagnostics,
        "concentration": concentration,
        "rolling_trade_windows": rolling_summary,
        "regime_contrasts": contrasts.replace({np.nan: None}).to_dict(orient="records"),
        "source_archives_validated": [str(sources["formal_replay_archive"]), str(sources["feature_archive"])],
        "forward_data_read": False,
        "strategy_parameters_changed": False,
        "candidate_created": False,
        "pte_mutated": False,
    }
    (artifacts / "attribution_evidence.json").write_text(
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
            "decision": "ATTRIBUTION_COMPLETE",
            "strategy_parameters_changed": False,
            "candidate_created": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
