from __future__ import annotations

import hashlib
import json
from datetime import time
from pathlib import Path

import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260913_S004_EX39"


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
        raise ValueError("EX39 may only run the fixed external replication")

    dataset = protocol["dataset"]
    upstream = protocol["upstream"]
    expected_hashes = {
        repo / str(dataset["replication_manifest"]): str(dataset["replication_manifest_sha256"]),
        repo / str(upstream["manifest"]): str(upstream["manifest_sha256"]),
        repo / str(upstream["evaluation"]): str(upstream["evaluation_sha256"]),
    }
    for path, digest in expected_hashes.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path}")
    validate_experiment_archive((repo / str(upstream["manifest"])).parent)
    source_result = _read_json(repo / str(upstream["evaluation"]))
    if source_result.get("route_decision") != "STOP_MARGIN_BEHAVIOR_FAMILY":
        raise ValueError("source result does not contain the negative direction to replicate")

    symbol = str(protocol["research_target"]["replication_symbol"])
    start = pd.Timestamp(str(dataset["start"]))
    cutoff = pd.Timestamp(str(dataset["development_cutoff"]))
    market = load_market_data(repo / str(dataset["market_directory"]), symbol)
    daily = market.daily.copy()
    daily["source_date"] = pd.to_datetime(daily["dt"]).dt.normalize()
    daily = daily.loc[daily["source_date"].between(start, cutoff)].sort_values("source_date")
    daily["same_day_return"] = daily["close"].astype(float).pct_change(fill_method=None)
    calendar = pd.DatetimeIndex(daily["source_date"].drop_duplicates())
    next_session = pd.Series(calendar[1:], index=calendar[:-1])

    margin = get_tushare_pro(repo / ".env").margin_detail(
        ts_code=symbol,
        start_date=start.strftime("%Y%m%d"),
        end_date=cutoff.strftime("%Y%m%d"),
    )
    if margin is None or margin.empty:
        raise ValueError("Tushare returned no replication margin_detail rows")
    margin = margin.copy()
    margin["trade_date"] = pd.to_datetime(
        margin["trade_date"].astype(str), format="%Y%m%d"
    ).dt.normalize()
    margin = margin.sort_values("trade_date").reset_index(drop=True)
    required = [str(value) for value in protocol["source"]["required_fields"]]
    missing_columns = sorted(set(required) - set(margin.columns))
    duplicates = int(margin.duplicated(["ts_code", "trade_date"], keep=False).sum())
    required_null_rows = (
        int(margin[required].isna().any(axis=1).sum()) if not missing_columns else -1
    )
    first_record = pd.Timestamp(margin["trade_date"].min())
    eligible_dates = calendar[calendar >= first_record]
    observed = set(margin["trade_date"])
    covered = sum(value in observed for value in eligible_dates)
    coverage = covered / len(eligible_dates)
    quality_gate = protocol["quality_gate"]
    data_pass = bool(
        not missing_columns
        and len(margin) >= int(quality_gate["minimum_rows"])
        and coverage >= float(quality_gate["minimum_coverage_from_first_record"])
        and duplicates <= int(quality_gate["maximum_duplicate_symbol_dates"])
        and required_null_rows <= int(quality_gate["maximum_required_null_rows"])
    )
    if not data_pass:
        raise ValueError("replication margin data gate failed")

    margin["source_date"] = margin["trade_date"]
    margin["net_financing_flow"] = (
        margin["rzmre"].astype(float) - margin["rzche"].astype(float)
    )
    margin["previous_rzye"] = margin["rzye"].astype(float).shift(1)
    margin["net_flow_ratio"] = margin["net_financing_flow"] / margin["previous_rzye"]
    margin["same_day_return"] = margin["source_date"].map(
        daily.set_index("source_date")["same_day_return"]
    )
    margin["event_date"] = margin["source_date"].map(next_session)
    margin = margin.loc[
        margin["event_date"].notna()
        & margin["event_date"].le(cutoff)
        & margin["previous_rzye"].gt(0)
        & margin["same_day_return"].notna()
    ].copy()
    cfg = protocol["feature"]
    margin["threshold"] = (
        margin["net_flow_ratio"]
        .rolling(int(cfg["rolling_sessions"]), min_periods=int(cfg["rolling_sessions"]))
        .quantile(float(cfg["quantile"]))
        .shift(1)
    )
    margin["event"] = (
        margin["threshold"].notna()
        & margin["net_financing_flow"].gt(0)
        & margin["net_flow_ratio"].ge(margin["threshold"])
        & margin["same_day_return"].le(float(cfg["same_day_return_maximum"]))
    )
    events = margin.loc[margin["event"]].copy()

    entry_price = daily.set_index("source_date")["open"].astype(float)
    intraday = market.intraday.copy()
    intraday["dt"] = pd.to_datetime(intraday["dt"])
    exit_price = intraday.loc[intraday["dt"].dt.time.eq(time(11, 30))].set_index(
        intraday.loc[intraday["dt"].dt.time.eq(time(11, 30)), "dt"].dt.normalize()
    )["close"].astype(float)
    events["entry_price"] = events["event_date"].map(entry_price)
    events["exit_price"] = events["event_date"].map(exit_price)
    if events[["entry_price", "exit_price"]].isna().any().any():
        raise ValueError("replication execution price missing")

    daily_regime = daily[["source_date", "close"]].copy()
    daily_return = daily_regime["close"].astype(float).pct_change(fill_method=None)
    daily_regime["known_volatility"] = daily_return.rolling(20, min_periods=20).std(ddof=1)
    daily_regime["known_volatility_median"] = daily_regime["known_volatility"].rolling(
        120, min_periods=120
    ).median().shift(1)
    daily_regime["low_volatility"] = daily_regime["known_volatility"].le(
        daily_regime["known_volatility_median"]
    )
    regime = daily_regime.set_index("source_date")
    events["low_volatility"] = events["source_date"].map(regime["low_volatility"])
    events["gross_return"] = events["exit_price"] / events["entry_price"] - 1.0
    cost = float(protocol["execution"]["stress_cost_bps_per_side"])
    events["stress_return"] = events["gross_return"] - 2.0 * cost / 10_000.0
    events["year"] = events["event_date"].dt.year
    low_vol = events.loc[events["low_volatility"].astype(bool)]
    recent_start = pd.Timestamp(calendar[-252])
    recent = events.loc[events["event_date"].ge(recent_start)]
    annual = events.groupby("year")["stress_return"].agg(trades="size", stress_mean="mean").reset_index()

    metrics = {
        "path_id": "ACCUM_Q60",
        "symbol": symbol,
        "trades": int(len(events)),
        "stress_mean": float(events["stress_return"].mean()),
        "profit_factor": _profit_factor(events["stress_return"]),
        "negative_years": int(annual["stress_mean"].lt(0).sum()),
        "evaluated_years": int(len(annual)),
        "low_vol_trades": int(len(low_vol)),
        "low_vol_stress_mean": float(low_vol["stress_return"].mean()),
        "low_vol_profit_factor": _profit_factor(low_vol["stress_return"]),
        "recent_252_start": recent_start.strftime("%Y-%m-%d"),
        "recent_252_trades": int(len(recent)),
        "recent_252_stress_mean": float(recent["stress_return"].mean()),
    }
    gate = protocol["replication_gate"]
    replicated = bool(
        metrics["stress_mean"] < float(gate["full_stress_mean_max_exclusive"])
        and metrics["profit_factor"] < float(gate["full_profit_factor_max_exclusive"])
        and metrics["low_vol_stress_mean"] < float(gate["low_vol_stress_mean_max_exclusive"])
        and metrics["low_vol_profit_factor"] < float(gate["low_vol_profit_factor_max_exclusive"])
        and metrics["negative_years"] >= int(gate["negative_years_minimum"])
        and metrics["recent_252_stress_mean"] < float(gate["recent_252_stress_mean_max_exclusive"])
    )
    metrics["replication_pass"] = replicated
    route = (
        "MARGIN_ABSORBED_BUYING_RISK_FILTER_REPLICATED"
        if replicated
        else "STOP_MARGIN_ABSORPTION_RISK_FILTER"
    )
    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS" if replicated else "FAIL",
        "route_decision": route,
        "candidate_created": False,
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    margin_out = margin.copy()
    events_out = events.copy()
    for frame in (margin_out, events_out):
        for column in ("trade_date", "source_date", "event_date"):
            frame[column] = pd.to_datetime(frame[column]).dt.strftime("%Y-%m-%d")
    margin_out.to_csv(artifacts / "replication_margin_detail.csv.gz", index=False, compression=compression)
    events_out.to_csv(artifacts / "replication_trades.csv.gz", index=False, compression=compression)
    pd.DataFrame([metrics]).to_csv(artifacts / "path_metrics.csv", index=False, lineterminator="\n")
    annual.to_csv(artifacts / "annual_metrics.csv", index=False, lineterminator="\n")
    _write_json(
        artifacts / "data_quality.json",
        {
            "status": "PASS",
            "rows": int(len(margin)),
            "first_record": first_record.strftime("%Y-%m-%d"),
            "coverage_from_first_record": float(coverage),
            "required_null_rows": required_null_rows,
            "duplicate_symbol_dates": duplicates,
        },
    )
    _write_json(artifacts / "evaluation.json", result)

    (experiment / "03_execution.md").write_text(
        "# S004 EX39 执行\n\n"
        f"510500融资数据门通过；固定规则形成{metrics['trades']}笔，压力均值"
        f"{metrics['stress_mean']:.3%}、盈亏比{metrics['profit_factor']:.2f}；低波动"
        f"{metrics['low_vol_trades']}笔、均值{metrics['low_vol_stress_mean']:.3%}、盈亏比"
        f"{metrics['low_vol_profit_factor']:.2f}；负收益年度"
        f"{metrics['negative_years']}/{metrics['evaluated_years']}，最近252个交易日"
        f"{metrics['recent_252_stress_mean']:.3%}。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX39 结论\n\n"
        f"结论：`{route}`。本轮固定迁移，没有在510500上重新选参，也没有创建候选或修改"
        "S004-C001、SM、PTE。\n",
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
