from __future__ import annotations

import hashlib
import json
from datetime import time
from pathlib import Path

import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260913_S004_EX31"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _profit_factor(returns: pd.Series) -> float:
    positive = float(returns.loc[returns > 0].sum())
    negative = float(-returns.loc[returns < 0].sum())
    return positive / negative if negative > 0 else float("inf")


def _build_absorption_features(
    moneyflow_panel: pd.DataFrame,
    price_panel: pd.DataFrame,
    daily: pd.DataFrame,
) -> pd.DataFrame:
    moneyflow = moneyflow_panel.copy()
    price = price_panel.copy()
    moneyflow["dt"] = pd.to_datetime(moneyflow["dt"]).dt.normalize()
    price["dt"] = pd.to_datetime(price["dt"]).dt.normalize()
    frame = moneyflow.merge(
        price[["dt", "con_code", "pct_chg"]],
        on=["dt", "con_code"],
        how="left",
        validate="one_to_one",
    )
    if frame["pct_chg"].isna().any():
        raise ValueError("observed moneyflow member lacks price return")
    observed = frame["observed_moneyflow"].astype(bool)
    frame["observed_weight"] = frame["weight"].where(observed, 0.0)
    frame["absorption"] = observed & frame["net_mf_amount"].gt(0) & frame["pct_chg"].le(0)
    frame["absorption_weight"] = frame["weight"].where(frame["absorption"], 0.0)
    grouped = frame.groupby("dt", sort=True)
    features = grouped[["weight", "observed_weight", "absorption_weight"]].sum()
    features["observed_weight_ratio"] = features["observed_weight"] / features["weight"]
    features["absorption_breadth"] = features["absorption_weight"] / features[
        "observed_weight"
    ]
    market = daily[["dt", "close"]].copy()
    market["dt"] = pd.to_datetime(market["dt"]).dt.normalize()
    market = market.sort_values("dt")
    market["etf_return_pct"] = market["close"].pct_change() * 100.0
    market["etf_volatility_20"] = market["etf_return_pct"].rolling(20, min_periods=20).std(ddof=1)
    market["prior_volatility_median_120"] = market["etf_volatility_20"].shift(1).rolling(
        120, min_periods=120
    ).median()
    market["low_volatility"] = (
        market["etf_volatility_20"] <= market["prior_volatility_median_120"]
    )
    return features.reset_index().merge(
        market[
            [
                "dt",
                "etf_return_pct",
                "etf_volatility_20",
                "prior_volatility_median_120",
                "low_volatility",
            ]
        ],
        on="dt",
        how="left",
        validate="one_to_one",
    )


def _generate_events(
    features: pd.DataFrame,
    spec: dict[str, object],
    evaluation_start: pd.Timestamp,
) -> pd.DataFrame:
    features = features.sort_values("dt").reset_index(drop=True)
    threshold = features["absorption_breadth"].shift(1).rolling(
        int(spec["threshold_lookback_valid_sessions"]),
        min_periods=int(spec["threshold_lookback_valid_sessions"]),
    ).quantile(float(spec["threshold_quantile"]))
    active = (
        features["absorption_breadth"].ge(threshold)
        & features["observed_weight_ratio"].ge(
            float(spec["minimum_observed_weight_ratio"])
        )
    ).fillna(False)
    calendar = pd.DatetimeIndex(features["dt"])
    next_session = pd.Series(calendar, index=calendar).shift(-1)
    events = features.loc[active].copy()
    events["event_date"] = events["dt"].map(next_session)
    return events.loc[events["event_date"].ge(evaluation_start)].reset_index(drop=True)


def _attach_returns(events: pd.DataFrame, market, cost_bps: float) -> pd.DataFrame:
    daily = market.daily[["dt", "open"]].copy()
    daily["dt"] = pd.to_datetime(daily["dt"]).dt.normalize()
    entry = daily.set_index("dt")["open"]
    intraday = market.intraday[["dt", "close"]].copy()
    intraday["dt"] = pd.to_datetime(intraday["dt"])
    intraday = intraday.loc[intraday["dt"].dt.time.eq(time(11, 30))]
    intraday["session"] = intraday["dt"].dt.normalize()
    exit_price = intraday.set_index("session")["close"]
    output = events.copy()
    output["event_date"] = pd.to_datetime(output["event_date"]).dt.normalize()
    output["entry_price"] = output["event_date"].map(entry)
    output["exit_price"] = output["event_date"].map(exit_price)
    if output[["entry_price", "exit_price"]].isna().any().any():
        raise ValueError("event execution price is missing")
    output["gross_return"] = output["exit_price"] / output["entry_price"] - 1.0
    output["stress_return"] = output["gross_return"] - 2 * cost_bps / 10_000.0
    output["year"] = output["event_date"].dt.year
    return output


def _density(events: pd.DataFrame, calendar: pd.DatetimeIndex, start: pd.Timestamp) -> dict[str, float]:
    activation = pd.Series(calendar.isin(events["event_date"]), index=calendar, dtype=int)
    rolling = activation.rolling(60, min_periods=60).sum()
    eligible = rolling.loc[rolling.index >= start].dropna()
    return {
        "rolling_60_median": float(eligible.median()),
        "rolling_60_p10": float(eligible.quantile(0.10)),
        "rolling_60_min": float(eligible.min()),
    }


def _metrics(
    symbol: str,
    trades: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    start: pd.Timestamp,
) -> tuple[dict[str, object], pd.DataFrame]:
    low_vol = trades.loc[trades["low_volatility"].astype(bool)]
    yearly = trades.groupby("year")["stress_return"].agg(["count", "mean"])
    yearly = yearly.rename(columns={"count": "trades", "mean": "stress_mean"}).reset_index()
    yearly.insert(0, "symbol", symbol)
    metrics: dict[str, object] = {
        "symbol": symbol,
        "trades": int(len(trades)),
        **_density(trades, calendar, start),
        "stress_mean": float(trades["stress_return"].mean()),
        "profit_factor": _profit_factor(trades["stress_return"]),
        "positive_years": int((yearly["stress_mean"] > 0).sum()),
        "evaluated_years": int(len(yearly)),
        "low_vol_trades": int(len(low_vol)),
        "low_vol_stress_mean": float(low_vol["stress_return"].mean()),
        "low_vol_profit_factor": _profit_factor(low_vol["stress_return"]),
        "recent_252_stress_mean": float(
            trades.loc[trades["event_date"].ge(calendar[-252]), "stress_return"].mean()
        ),
    }
    return metrics, yearly


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("candidate_generation") or protocol.get("promotion_allowed"):
        raise ValueError("EX31 may only evaluate the frozen mechanism")

    sources = protocol["sources"]
    primary_source = repo / "experiments" / "S004" / sources["primary_event_experiment"]
    replication_moneyflow = (
        repo / "experiments" / "S003" / sources["replication_moneyflow_experiment"]
    )
    replication_price = (
        repo / "experiments" / "S003" / sources["replication_price_experiment"]
    )
    for source in (primary_source, replication_moneyflow, replication_price):
        validate_experiment_archive(source)
    expected = {
        primary_source / "experiment_manifest.json": sources["primary_manifest_sha256"],
        primary_source / "artifacts" / "mechanism_events.csv.gz": sources[
            "primary_events_sha256"
        ],
        replication_moneyflow / "experiment_manifest.json": sources[
            "replication_moneyflow_manifest_sha256"
        ],
        replication_moneyflow / "artifacts" / "constituent_moneyflow_panel.csv.gz": sources[
            "replication_moneyflow_panel_sha256"
        ],
        replication_price / "experiment_manifest.json": sources[
            "replication_price_manifest_sha256"
        ],
        replication_price / "artifacts" / "lifecycle_aware_constituent_panel.csv.gz": sources[
            "replication_price_panel_sha256"
        ],
        repo / sources["primary_market_manifest"]: sources["primary_market_manifest_sha256"],
        repo / sources["replication_market_manifest"]: sources[
            "replication_market_manifest_sha256"
        ],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")

    cutoff = pd.Timestamp(protocol["dataset"]["development_cutoff"])
    evaluation_start = pd.Timestamp(protocol["dataset"]["evaluation_start"])
    cost = float(protocol["execution"]["stress_cost_bps_per_side"])
    frozen_path = protocol["research_target"]["frozen_path"]

    primary_market = load_market_data(repo / "data" / "raw", "588080.SH")
    primary_events = pd.read_csv(primary_source / "artifacts" / "mechanism_events.csv.gz")
    primary_events = primary_events.loc[primary_events["path_id"].eq(frozen_path)].copy()
    primary_events["dt"] = pd.to_datetime(primary_events["dt"])
    primary_events["event_date"] = pd.to_datetime(primary_events["event_date"])
    primary_events = primary_events.loc[primary_events["event_date"].le(cutoff)]
    primary_trades = _attach_returns(primary_events, primary_market, cost)

    replication_market = load_market_data(repo / "data" / "raw", "510500.SH")
    replication_moneyflow_panel = pd.read_csv(
        replication_moneyflow / "artifacts" / "constituent_moneyflow_panel.csv.gz"
    )
    replication_price_panel = pd.read_csv(
        replication_price / "artifacts" / "lifecycle_aware_constituent_panel.csv.gz"
    )
    replication_features = _build_absorption_features(
        replication_moneyflow_panel, replication_price_panel, replication_market.daily
    )
    replication_features = replication_features.loc[replication_features["dt"].le(cutoff)]
    replication_events = _generate_events(
        replication_features, protocol["feature"], evaluation_start
    )
    replication_trades = _attach_returns(replication_events, replication_market, cost)

    trade_frames: list[pd.DataFrame] = []
    metric_rows: list[dict[str, object]] = []
    yearly_frames: list[pd.DataFrame] = []
    for symbol, trades, market in (
        ("588080.SH", primary_trades, primary_market),
        ("510500.SH", replication_trades, replication_market),
    ):
        calendar = pd.DatetimeIndex(pd.to_datetime(market.daily["dt"]).dt.normalize())
        calendar = calendar[calendar <= cutoff]
        metrics, yearly = _metrics(symbol, trades, calendar, evaluation_start)
        trades = trades.copy()
        trades.insert(0, "symbol", symbol)
        trade_frames.append(trades)
        metric_rows.append(metrics)
        yearly_frames.append(yearly)

    metrics = pd.DataFrame(metric_rows)
    gate = protocol["acceptance"]
    metrics["density_pass"] = (
        metrics["rolling_60_median"].between(
            float(gate["rolling_60_median_min"]),
            float(gate["rolling_60_median_max"]),
        )
        & metrics["rolling_60_p10"].ge(float(gate["rolling_60_p10_min"]))
    )
    metrics["quality_pass"] = (
        metrics["stress_mean"].gt(float(gate["full_stress_mean_min_exclusive"]))
        & metrics["profit_factor"].gt(float(gate["full_profit_factor_min_exclusive"]))
        & metrics["low_vol_stress_mean"].gt(
            float(gate["low_vol_stress_mean_min_exclusive"])
        )
        & metrics["low_vol_profit_factor"].gt(
            float(gate["low_vol_profit_factor_min_exclusive"])
        )
        & metrics["positive_years"].ge(int(gate["positive_years_min"]))
    )
    passed = bool(metrics["density_pass"].all() and metrics["quality_pass"].all())
    metrics.to_csv(artifacts / "symbol_metrics.csv", index=False, lineterminator="\n")
    pd.concat(yearly_frames, ignore_index=True).to_csv(
        artifacts / "yearly_metrics.csv", index=False, lineterminator="\n"
    )
    trades_out = pd.concat(trade_frames, ignore_index=True)
    for column in ("dt", "event_date"):
        trades_out[column] = pd.to_datetime(trades_out[column]).dt.strftime("%Y-%m-%d")
    trades_out.to_csv(
        artifacts / "trades.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS" if passed else "FAIL",
        "frozen_path": frozen_path,
        "cumulative_return_bearing_trials": int(protocol["prior_return_bearing_trials"])
        + int(protocol["return_bearing_trials_added"]),
        "route_decision": "CONTINUE_MECHANISM_AUDIT" if passed else "STOP_ABSORPTION_FAMILY",
        "candidate_created": False,
    }
    _write_json(artifacts / "evaluation_summary.json", summary)

    lines = []
    for row in metrics.itertuples(index=False):
        lines.append(
            f"- {row.symbol}：{row.trades}笔，滚动60日中位数{row.rolling_60_median:.1f}/"
            f"P10 {row.rolling_60_p10:.1f}；全样本压力均值{row.stress_mean:.3%}、"
            f"盈亏比{row.profit_factor:.2f}；低波动{row.low_vol_trades}笔、压力均值"
            f"{row.low_vol_stress_mean:.3%}、盈亏比{row.low_vol_profit_factor:.2f}；"
            f"正收益年度{row.positive_years}/{row.evaluated_years}。"
        )
    (experiment / "03_execution.md").write_text(
        "# S004 EX31 执行\n\n"
        f"固定成分吸筹规则收益评价：`{summary['status']}`。\n\n"
        + "\n".join(lines)
        + "\n\n本轮是第69次收益检验，没有改变阈值或执行时点。\n",
        encoding="utf-8",
    )
    conclusion = (
        "双标的全样本与低波动子样本均通过，可进入统计和竞争解释审计。"
        if passed
        else "至少一个预注册必要条件失败，停止成分级吸筹机制，不创建S004-C002。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX31 结论\n\n"
        f"结论：`{summary['route_decision']}`。{conclusion}没有修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S004",
            "symbol": "588080.SH",
            "development_cutoff": protocol["dataset"]["development_cutoff"],
            "status": summary["status"],
            "route_decision": summary["route_decision"],
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
