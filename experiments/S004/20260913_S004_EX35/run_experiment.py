from __future__ import annotations

import hashlib
import json
from datetime import time
from pathlib import Path

import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260913_S004_EX35"


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _profit_factor(returns: pd.Series) -> float:
    gains = float(returns.loc[returns > 0].sum())
    losses = float(-returns.loc[returns < 0].sum())
    return gains / losses if losses > 0 else float("inf")


def _attach_causal_regime(daily: pd.DataFrame, cfg: dict[str, object]) -> pd.DataFrame:
    frame = daily[["dt", "close"]].copy().sort_values("dt")
    frame["dt"] = pd.to_datetime(frame["dt"]).dt.normalize()
    returns = frame["close"].pct_change()
    realized = returns.rolling(
        int(cfg["volatility_sessions"]), min_periods=int(cfg["volatility_sessions"])
    ).std(ddof=1)
    frame["known_volatility"] = realized.shift(1)
    frame["known_volatility_median"] = frame["known_volatility"].rolling(
        int(cfg["trailing_median_sessions"]),
        min_periods=int(cfg["trailing_median_sessions"]),
    ).median()
    frame["low_volatility"] = frame["known_volatility"].le(
        frame["known_volatility_median"]
    )
    return frame[["dt", "known_volatility", "known_volatility_median", "low_volatility"]]


def _path_metrics(
    path_id: str,
    trades: pd.DataFrame,
    recent_start: pd.Timestamp,
) -> tuple[dict[str, object], pd.DataFrame]:
    low_vol = trades.loc[trades["low_volatility"].astype(bool)]
    yearly = (
        trades.groupby("year")["stress_return"]
        .agg(trades="size", stress_mean="mean")
        .reset_index()
    )
    yearly.insert(0, "path_id", path_id)
    recent = trades.loc[trades["event_date"].ge(recent_start), "stress_return"]
    return (
        {
            "path_id": path_id,
            "trades": int(len(trades)),
            "gross_mean": float(trades["gross_return"].mean()),
            "stress_mean": float(trades["stress_return"].mean()),
            "profit_factor": _profit_factor(trades["stress_return"]),
            "positive_years": int(yearly["stress_mean"].gt(0).sum()),
            "evaluated_years": int(len(yearly)),
            "low_vol_trades": int(len(low_vol)),
            "low_vol_stress_mean": float(low_vol["stress_return"].mean()),
            "low_vol_profit_factor": _profit_factor(low_vol["stress_return"]),
            "recent_252_trades": int(len(recent)),
            "recent_252_stress_mean": float(recent.mean()),
        },
        yearly,
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX35 may only evaluate the frozen competing hypotheses")

    dataset = protocol["dataset"]
    upstream = protocol["upstream"]
    expected_hashes = {
        repo / str(dataset["market_manifest"]): str(dataset["market_manifest_sha256"]),
        repo / str(upstream["manifest"]): str(upstream["manifest_sha256"]),
        repo / str(upstream["event_panel"]): str(upstream["event_panel_sha256"]),
        repo / str(upstream["selection"]): str(upstream["selection_sha256"]),
    }
    for path, digest in expected_hashes.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path}")
    validate_experiment_archive((repo / str(upstream["manifest"])).parent)
    selection = _read_json(repo / str(upstream["selection"]))
    fixed_paths = [str(value) for value in protocol["research_target"]["fixed_paths"]]
    if set(selection["selected_paths"].values()) != set(fixed_paths):
        raise ValueError("fixed paths differ from density-only selection")

    market = load_market_data(
        repo / str(dataset["market_directory"]),
        str(protocol["research_target"]["trade_symbol"]),
    )
    cutoff = pd.Timestamp(str(dataset["development_cutoff"]))
    start = pd.Timestamp(str(dataset["evaluation_start"]))
    daily = market.daily.loc[market.daily["dt"].between(start, cutoff)].copy()
    trading_dates = pd.DatetimeIndex(
        pd.to_datetime(daily["dt"]).dt.normalize().drop_duplicates().sort_values()
    )
    if len(trading_dates) < 252:
        raise ValueError("fewer than 252 A-share sessions in the evaluation window")
    recent_start = pd.Timestamp(trading_dates[-252])
    entry = daily.set_index(pd.to_datetime(daily["dt"]).dt.normalize())["open"].astype(float)
    intraday = market.intraday.copy()
    intraday["dt"] = pd.to_datetime(intraday["dt"])
    exit_bars = intraday.loc[intraday["dt"].dt.time.eq(time(11, 30))].copy()
    exit_price = exit_bars.set_index(exit_bars["dt"].dt.normalize())["close"].astype(float)
    regimes = _attach_causal_regime(market.daily, protocol["regime"]).set_index("dt")

    event_panel = pd.read_csv(repo / str(upstream["event_panel"]), compression="gzip")
    event_panel["a_share_date"] = pd.to_datetime(event_panel["a_share_date"]).dt.normalize()
    event_panel["us_trade_date"] = pd.to_datetime(event_panel["us_trade_date"]).dt.normalize()
    event_panel = event_panel.loc[event_panel["a_share_date"].between(start, cutoff)].copy()
    cost = float(protocol["execution"]["stress_cost_bps_per_side"])
    all_trades: list[pd.DataFrame] = []
    metric_rows: list[dict[str, object]] = []
    yearly_frames: list[pd.DataFrame] = []
    for path_id in fixed_paths:
        events = event_panel.loc[event_panel[path_id].astype(bool)].copy()
        events["entry_price"] = events["a_share_date"].map(entry)
        events["exit_price"] = events["a_share_date"].map(exit_price)
        events["known_volatility"] = events["a_share_date"].map(regimes["known_volatility"])
        events["known_volatility_median"] = events["a_share_date"].map(
            regimes["known_volatility_median"]
        )
        events["low_volatility"] = events["a_share_date"].map(regimes["low_volatility"])
        if events[["entry_price", "exit_price"]].isna().any().any():
            raise ValueError(f"execution price missing for {path_id}")
        events["gross_return"] = events["exit_price"] / events["entry_price"] - 1.0
        events["stress_return"] = events["gross_return"] - 2.0 * cost / 10_000.0
        events["event_date"] = events["a_share_date"]
        events["year"] = events["event_date"].dt.year
        events.insert(0, "path_id", path_id)
        metrics, yearly = _path_metrics(path_id, events, recent_start)
        all_trades.append(events)
        metric_rows.append(metrics)
        yearly_frames.append(yearly)

    metrics = pd.DataFrame(metric_rows)
    gate = protocol["acceptance"]
    metrics["quality_pass"] = (
        metrics["stress_mean"].gt(float(gate["full_stress_mean_min_exclusive"]))
        & metrics["profit_factor"].gt(float(gate["full_profit_factor_min_exclusive"]))
        & metrics["low_vol_stress_mean"].gt(float(gate["low_vol_stress_mean_min_exclusive"]))
        & metrics["low_vol_profit_factor"].gt(float(gate["low_vol_profit_factor_min_exclusive"]))
        & metrics["positive_years"].ge(int(gate["positive_years_minimum"]))
        & metrics["recent_252_stress_mean"].gt(
            float(gate["recent_252_stress_mean_min_exclusive"])
        )
    )
    qualified = metrics.loc[metrics["quality_pass"], "path_id"].astype(str).tolist()
    if qualified:
        route_decision = "PROCEED_TO_OVERSEAS_TRANSMISSION_STATISTICAL_AUDIT"
        interpretation = "至少一条长仓路径支持可执行的信息延续或过度反应修复"
    else:
        route_decision = "STOP_OVERSEAS_TRANSMISSION_FAMILY"
        interpretation = "开盘后没有满足预注册质量门的长仓条件收益"
    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS" if qualified else "FAIL",
        "qualified_paths": qualified,
        "route_decision": route_decision,
        "interpretation": interpretation,
        "candidate_created": False,
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    trades_out = pd.concat(all_trades, ignore_index=True)
    for column in ("a_share_date", "us_trade_date", "event_date"):
        trades_out[column] = trades_out[column].dt.strftime("%Y-%m-%d")
    trades_out.to_csv(artifacts / "trades.csv.gz", index=False, compression=compression)
    metrics.to_csv(artifacts / "path_metrics.csv", index=False, lineterminator="\n")
    pd.concat(yearly_frames, ignore_index=True).to_csv(
        artifacts / "annual_metrics.csv", index=False, lineterminator="\n"
    )
    _write_json(artifacts / "evaluation.json", result)

    rows = []
    for row in metrics.itertuples(index=False):
        rows.append(
            f"- {row.path_id}：{row.trades}笔，压力均值{row.stress_mean:.3%}，"
            f"盈亏比{row.profit_factor:.2f}；低波动{row.low_vol_trades}笔、"
            f"均值{row.low_vol_stress_mean:.3%}、盈亏比{row.low_vol_profit_factor:.2f}；"
            f"正收益年度{row.positive_years}/{row.evaluated_years}，最近252日"
            f"{row.recent_252_stress_mean:.3%}，质量门{'通过' if row.quality_pass else '失败'}。"
        )
    (experiment / "03_execution.md").write_text(
        "# S004 EX35 执行\n\n状态：`COMPLETE`。\n\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX35 结论\n\n"
        f"结论：`{route_decision}`。{interpretation}。"
        "本轮固定评价两个竞争假设，没有回改事件定义、创建候选或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S004",
            "symbol": "588080.SH",
            "development_cutoff": str(dataset["development_cutoff"]),
            "status": "COMPLETE",
            "route_decision": route_decision,
            "conditional_return_analysis": True,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
