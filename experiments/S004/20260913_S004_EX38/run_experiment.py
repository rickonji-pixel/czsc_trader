from __future__ import annotations

import hashlib
import json
from datetime import time
from pathlib import Path

import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260913_S004_EX38"


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


def _causal_regime(daily: pd.DataFrame, cfg: dict[str, object]) -> pd.DataFrame:
    frame = daily[["dt", "close"]].copy().sort_values("dt")
    frame["source_date"] = pd.to_datetime(frame["dt"]).dt.normalize()
    returns = frame["close"].astype(float).pct_change(fill_method=None)
    frame["known_volatility"] = returns.rolling(
        int(cfg["volatility_sessions"]), min_periods=int(cfg["volatility_sessions"])
    ).std(ddof=1)
    frame["known_volatility_median"] = frame["known_volatility"].rolling(
        int(cfg["trailing_median_sessions"]),
        min_periods=int(cfg["trailing_median_sessions"]),
    ).median().shift(1)
    frame["low_volatility"] = frame["known_volatility"].le(
        frame["known_volatility_median"]
    )
    return frame[
        ["source_date", "known_volatility", "known_volatility_median", "low_volatility"]
    ]


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        protocol.get(key)
        for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    ):
        raise ValueError("EX38 may only evaluate the frozen margin behavior path")

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
    path_id = str(protocol["research_target"]["fixed_path"])
    if path_id not in selection.get("selected_paths", {}).values():
        raise ValueError("fixed path differs from density-only selection")

    symbol = str(protocol["research_target"]["trade_symbol"])
    cutoff = pd.Timestamp(str(dataset["development_cutoff"]))
    market = load_market_data(repo / str(dataset["market_directory"]), symbol)
    daily = market.daily.copy()
    daily["event_date"] = pd.to_datetime(daily["dt"]).dt.normalize()
    daily = daily.loc[daily["event_date"].le(cutoff)].sort_values("event_date")
    trading_dates = pd.DatetimeIndex(daily["event_date"].drop_duplicates())
    if len(trading_dates) < 252:
        raise ValueError("fewer than 252 A-share sessions in the evaluation window")
    recent_start = pd.Timestamp(trading_dates[-252])
    entry_price = daily.set_index("event_date")["open"].astype(float)
    intraday = market.intraday.copy()
    intraday["dt"] = pd.to_datetime(intraday["dt"])
    exit_bars = intraday.loc[intraday["dt"].dt.time.eq(time(11, 30))].copy()
    exit_price = exit_bars.set_index(exit_bars["dt"].dt.normalize())["close"].astype(float)
    regimes = _causal_regime(market.daily, protocol["regime"]).set_index("source_date")

    events = pd.read_csv(repo / str(upstream["event_panel"]), compression="gzip")
    events["source_date"] = pd.to_datetime(events["source_date"]).dt.normalize()
    events["event_date"] = pd.to_datetime(events["event_date"]).dt.normalize()
    events = events.loc[events[path_id].astype(bool) & events["event_date"].le(cutoff)].copy()
    events["entry_price"] = events["event_date"].map(entry_price)
    events["exit_price"] = events["event_date"].map(exit_price)
    events["known_volatility"] = events["source_date"].map(regimes["known_volatility"])
    events["known_volatility_median"] = events["source_date"].map(
        regimes["known_volatility_median"]
    )
    events["low_volatility"] = events["source_date"].map(regimes["low_volatility"])
    if events[["entry_price", "exit_price"]].isna().any().any():
        raise ValueError("execution price missing")
    events["gross_return"] = events["exit_price"] / events["entry_price"] - 1.0
    cost = float(protocol["execution"]["stress_cost_bps_per_side"])
    events["stress_return"] = events["gross_return"] - 2.0 * cost / 10_000.0
    events["year"] = events["event_date"].dt.year

    low_vol = events.loc[events["low_volatility"].astype(bool)]
    recent = events.loc[events["event_date"].ge(recent_start)]
    annual = (
        events.groupby("year")["stress_return"]
        .agg(trades="size", stress_mean="mean")
        .reset_index()
    )
    metrics = {
        "path_id": path_id,
        "trades": int(len(events)),
        "gross_mean": float(events["gross_return"].mean()),
        "stress_mean": float(events["stress_return"].mean()),
        "profit_factor": _profit_factor(events["stress_return"]),
        "positive_years": int(annual["stress_mean"].gt(0).sum()),
        "evaluated_years": int(len(annual)),
        "low_vol_trades": int(len(low_vol)),
        "low_vol_stress_mean": float(low_vol["stress_return"].mean()),
        "low_vol_profit_factor": _profit_factor(low_vol["stress_return"]),
        "recent_252_start": recent_start.strftime("%Y-%m-%d"),
        "recent_252_trades": int(len(recent)),
        "recent_252_stress_mean": float(recent["stress_return"].mean()),
    }
    gate = protocol["acceptance"]
    quality_pass = bool(
        metrics["stress_mean"] > float(gate["full_stress_mean_min_exclusive"])
        and metrics["profit_factor"] > float(gate["full_profit_factor_min_exclusive"])
        and metrics["low_vol_stress_mean"] > float(gate["low_vol_stress_mean_min_exclusive"])
        and metrics["low_vol_profit_factor"] > float(gate["low_vol_profit_factor_min_exclusive"])
        and metrics["positive_years"] >= int(gate["positive_years_minimum"])
        and metrics["recent_252_stress_mean"] > float(gate["recent_252_stress_mean_min_exclusive"])
    )
    metrics["quality_pass"] = quality_pass
    route = (
        "PROCEED_TO_MARGIN_BEHAVIOR_CROSS_SECTION_REPLICATION"
        if quality_pass
        else "STOP_MARGIN_BEHAVIOR_FAMILY"
    )
    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS" if quality_pass else "FAIL",
        "qualified_path": path_id if quality_pass else None,
        "route_decision": route,
        "candidate_created": False,
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    output = events.copy()
    for column in ("trade_date", "source_date", "event_date"):
        output[column] = pd.to_datetime(output[column]).dt.strftime("%Y-%m-%d")
    output.to_csv(artifacts / "trades.csv.gz", index=False, compression=compression)
    pd.DataFrame([metrics]).to_csv(
        artifacts / "path_metrics.csv", index=False, lineterminator="\n"
    )
    annual.to_csv(artifacts / "annual_metrics.csv", index=False, lineterminator="\n")
    _write_json(artifacts / "evaluation.json", result)

    (experiment / "03_execution.md").write_text(
        "# S004 EX38 执行\n\n"
        f"状态：`COMPLETE`。{path_id}共{metrics['trades']}笔，压力均值"
        f"{metrics['stress_mean']:.3%}、盈亏比{metrics['profit_factor']:.2f}；低波动"
        f"{metrics['low_vol_trades']}笔、均值{metrics['low_vol_stress_mean']:.3%}、盈亏比"
        f"{metrics['low_vol_profit_factor']:.2f}；正收益年度"
        f"{metrics['positive_years']}/{metrics['evaluated_years']}，最近252个交易日"
        f"{metrics['recent_252_stress_mean']:.3%}，质量门{'通过' if quality_pass else '失败'}。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX38 结论\n\n"
        f"结论：`{route}`。本轮是累计第72条收益路径，没有回改事件定义、创建候选或修改"
        "SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S004",
            "symbol": symbol,
            "development_cutoff": str(dataset["development_cutoff"]),
            "status": "COMPLETE",
            "route_decision": route,
            "conditional_return_analysis": True,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
