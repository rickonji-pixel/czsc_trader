from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.intraday_opening_execution import _performance, _trade_return
from czsc_trader.intraday_opportunity_map import _price_checkpoints


EXPERIMENT_ID = "20260911_S003_EX52"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _load_minute(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    return frame.rename(
        columns={
            "datetime": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
            "amount": "Amount",
        }
    ).assign(Date=lambda value: pd.to_datetime(value["Date"]))


def _primary_returns(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    frame = frame.loc[frame["variant"].eq("PRIMARY_LONG"), ["event_date", "stress_return"]].copy()
    frame["event_date"] = pd.to_datetime(frame["event_date"])
    frame["month"] = frame["event_date"].dt.to_period("M")
    return frame


def _joint_month_bootstrap(
    by_market: dict[str, pd.DataFrame], *, block_months: int, replications: int, seed: int
) -> tuple[pd.DataFrame, dict[str, float]]:
    first = max(frame["month"].min() for frame in by_market.values())
    last = min(frame["month"].max() for frame in by_market.values())
    months = list(pd.period_range(first, last, freq="M"))
    lookup = {
        market: {month: rows["stress_return"].to_numpy(dtype=float) for month, rows in frame.groupby("month")}
        for market, frame in by_market.items()
    }
    rng = np.random.default_rng(seed)
    rows: list[dict[str, float]] = []
    blocks_needed = math.ceil(len(months) / block_months)
    for _ in range(replications):
        starts = rng.integers(0, len(months), size=blocks_needed)
        sampled = [months[(start + offset) % len(months)] for start in starts for offset in range(block_months)]
        sampled = sampled[: len(months)]
        means: dict[str, float] = {}
        for market in sorted(by_market):
            values = [lookup[market][month] for month in sampled if month in lookup[market]]
            means[market] = float(np.concatenate(values).mean())
        rows.append({**means, "pooled_equal_market_mean": float(np.mean(list(means.values())))})
    distribution = pd.DataFrame(rows)
    summary = {
        "pooled_lower_90": float(distribution["pooled_equal_market_mean"].quantile(0.05)),
        "pooled_median": float(distribution["pooled_equal_market_mean"].median()),
        "pooled_upper_90": float(distribution["pooled_equal_market_mean"].quantile(0.95)),
        "both_markets_positive_probability": float(
            distribution[list(sorted(by_market))].gt(0).all(axis=1).mean()
        ),
    }
    return distribution, summary


def _threshold_metrics(
    symbol: str,
    features_path: Path,
    minute: pd.DataFrame,
    quantiles: list[float],
    lookback: int,
    cost: float,
) -> list[dict[str, object]]:
    features = pd.read_csv(features_path)
    features["dt"] = pd.to_datetime(features["dt"]).dt.normalize()
    features = features.set_index("dt").sort_index()
    checkpoints = _price_checkpoints(minute)
    calendar = pd.DatetimeIndex(checkpoints.index)
    next_session = pd.Series(calendar[1:], index=calendar[:-1])
    rows: list[dict[str, object]] = []
    for quantile in quantiles:
        threshold = (
            features["moneyflow_breadth"].shift(1).rolling(lookback, min_periods=lookback).quantile(quantile)
        )
        signals = features.index[features["moneyflow_breadth"].ge(threshold)]
        event_dates = pd.DatetimeIndex(pd.Series(signals, index=signals).map(next_session).dropna().to_numpy())
        entry = checkpoints["OPEN"].reindex(event_dates)
        exit_price = checkpoints["11:30_CLOSE"].reindex(event_dates)
        valid = entry.notna() & exit_price.notna()
        returns = pd.Series(
            [
                _trade_return(float(entry_value), float(exit_value), 1, cost)
                for entry_value, exit_value in zip(entry.loc[valid], exit_price.loc[valid], strict=True)
            ],
            dtype=float,
        )
        performance = _performance(returns, 1.0)
        rows.append(
            {
                "symbol": symbol,
                "threshold_quantile": quantile,
                "events": int(len(returns)),
                "stress_mean_return": float(performance["mean_return"]),
                "stress_profit_factor": float(performance["profit_factor"]),
                "positive": bool(float(performance["mean_return"]) > 0),
            }
        )
    return rows


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        protocol.get(key)
        for key in (
            "parameter_selection",
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX52 may only synthesize frozen evidence")

    paths = {
        "510500_evaluation_manifest_sha256": repo / "experiments/20260911_S003_EX45/experiment_manifest.json",
        "510500_episodes_sha256": repo / "experiments/20260911_S003_EX45/artifacts/episodes.csv.gz",
        "510500_metrics_sha256": repo / "experiments/20260911_S003_EX45/artifacts/mechanism_metrics.csv",
        "510500_features_sha256": repo / "experiments/20260911_S003_EX44/artifacts/moneyflow_breadth_features.csv.gz",
        "510500_intraday_manifest_sha256": repo / "data/raw/510500_intraday_manifest.json",
        "512100_evaluation_manifest_sha256": repo / "experiments/20260911_S003_EX51/experiment_manifest.json",
        "512100_episodes_sha256": repo / "experiments/20260911_S003_EX51/artifacts/episodes.csv.gz",
        "512100_metrics_sha256": repo / "experiments/20260911_S003_EX51/artifacts/mechanism_metrics.csv",
        "512100_features_sha256": repo / "experiments/20260911_S003_EX50/artifacts/moneyflow_breadth_features.csv.gz",
        "512100_intraday_sha256": repo / "experiments/20260911_S003_EX48/artifacts/512100_5m.csv.gz",
        "ex47_statistical_summary_sha256": repo / "experiments/20260911_S003_EX47/artifacts/corrected_statistical_summary.json",
    }
    for key, path in paths.items():
        if _sha256(path) != protocol["sources"][key]:
            raise ValueError(f"source evidence hash differs: {path}")
    validate_experiment_archive(repo / "experiments/20260911_S003_EX45")
    validate_experiment_archive(repo / "experiments/20260911_S003_EX51")

    returns = {
        "510500.SH": _primary_returns(paths["510500_episodes_sha256"]),
        "512100.SH": _primary_returns(paths["512100_episodes_sha256"]),
    }
    bootstrap_spec = protocol["bootstrap"]
    distribution, bootstrap = _joint_month_bootstrap(
        returns,
        block_months=int(bootstrap_spec["block_months"]),
        replications=int(bootstrap_spec["replications"]),
        seed=int(bootstrap_spec["seed"]),
    )
    distribution.to_csv(
        artifacts / "joint_month_bootstrap.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )

    neighborhood = protocol["threshold_neighborhood"]
    minute_510500 = load_intraday_research_data(repo / "data/raw", "510500.SH").frames["5m"]
    minute_512100 = _load_minute(paths["512100_intraday_sha256"])
    threshold_rows = _threshold_metrics(
        "510500.SH",
        paths["510500_features_sha256"],
        minute_510500,
        [float(value) for value in neighborhood["quantiles"]],
        int(neighborhood["lookback_valid_sessions"]),
        float(neighborhood["stress_one_way_cost"]),
    ) + _threshold_metrics(
        "512100.SH",
        paths["512100_features_sha256"],
        minute_512100,
        [float(value) for value in neighborhood["quantiles"]],
        int(neighborhood["lookback_valid_sessions"]),
        float(neighborhood["stress_one_way_cost"]),
    )
    threshold_frame = pd.DataFrame(threshold_rows)
    threshold_frame.to_csv(artifacts / "threshold_neighborhood.csv", index=False, lineterminator="\n")

    metrics_510500 = pd.read_csv(paths["510500_metrics_sha256"]).iloc[0]
    metrics_512100 = pd.read_csv(paths["512100_metrics_sha256"]).iloc[0]
    ex47 = _read_json(paths["ex47_statistical_summary_sha256"])
    cross_market_pass = bool(
        bool(metrics_510500["eligible_for_audit"])
        and bool(metrics_512100["eligible_for_audit"])
        and bootstrap["pooled_lower_90"]
        > float(bootstrap_spec["pooled_mean_lower_bound_min_exclusive"])
        and bootstrap["both_markets_positive_probability"]
        >= float(bootstrap_spec["both_markets_positive_probability_min"])
        and bool(threshold_frame["positive"].all())
    )
    dsr_adverse = ex47["direction_flags"]["dsr"] == "ADVERSE"
    evidence = "MIXED" if cross_market_pass and dsr_adverse else "ADVERSE"
    eligible = bool(evidence == "MIXED")
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "cross_market_pass": cross_market_pass,
        "evidence_label": evidence,
        "pooled_equal_market_observed_mean": float(
            np.mean([frame["stress_return"].mean() for frame in returns.values()])
        ),
        **bootstrap,
        "all_threshold_neighborhood_means_positive": bool(threshold_frame["positive"].all()),
        "ex47_dsr_effective_probability": ex47["dsr_effective_probability"],
        "ex47_dsr_direction": ex47["direction_flags"]["dsr"],
        "eligible_for_candidate_workflow": eligible,
        "candidate_created": False,
    }
    _write_json(artifacts / "synthesis_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        "# S003 EX52 执行\n\n"
        f"跨市场综合门：`{'PASS' if cross_market_pass else 'FAIL'}`。两市场等权压力收益"
        f"均值{summary['pooled_equal_market_observed_mean']:.3%}；3个月联合区块Bootstrap的"
        f"90%区间为[{bootstrap['pooled_lower_90']:.3%}, {bootstrap['pooled_upper_90']:.3%}]，"
        f"两市场同时为正概率{bootstrap['both_markets_positive_probability']:.2%}。"
        f"q75/q80/q85共6个市场邻域的收益均值全部为正："
        f"{summary['all_threshold_neighborhood_means_positive']}。\n\n"
        f"EX47有效试验数口径DSR概率仍为{summary['ex47_dsr_effective_probability']:.2%}，"
        f"因此综合证据标签为`{evidence}`。\n",
        encoding="utf-8",
    )
    conclusion = (
        "独立横截面复现显著抬高了机制可信度，S003可进入候选定义、PK和体检流程；"
        "统计显著性不足继续作为正式风险标签，当前未创建候选。"
        if eligible
        else "跨市场共同下界不足，S003停止，不创建候选。"
    )
    (experiment / "04_conclusion.md").write_text(
        f"# S003 EX52 结论\n\n结论：`{evidence}`。{conclusion}本轮没有修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": "510500.SH",
            "development_cutoff": "2026-09-08",
            "status": "PASS" if cross_market_pass else "FAIL",
            "evidence_label": evidence,
            "eligible_for_candidate_workflow": eligible,
            "candidate_generation": False,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
