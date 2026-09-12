from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest
from czsc_trader.intraday_data import load_intraday_research_data


EXPERIMENT_ID = "20260912_S004_EX08"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _efficiency(frame: pd.DataFrame) -> float:
    returns = frame["Close"].pct_change(fill_method=None)
    first = float(frame.iloc[0]["Close"] / frame.iloc[0]["Open"] - 1.0)
    variation = float(returns.iloc[1:].abs().sum() + abs(first))
    displacement = abs(float(frame.iloc[-1]["Close"] / frame.iloc[0]["Open"] - 1.0))
    return displacement / variation if variation > 0.0 else 0.0


def _features(one_minute: pd.DataFrame) -> pd.DataFrame:
    bars = one_minute.copy()
    bars["Date"] = pd.to_datetime(bars["Date"], errors="raise")
    bars["trade_date"] = bars["Date"].dt.normalize()
    rows: list[dict[str, object]] = []
    for trade_date, day in bars.groupby("trade_date", sort=True, observed=True):
        if len(day) != 240:
            raise ValueError(f"{trade_date.date()}: incomplete minute session")
        previous = day.iloc[-120:-60]
        late = day.iloc[-60:]
        previous_return = float(previous.iloc[-1]["Close"] / previous.iloc[0]["Open"] - 1.0)
        late_return = float(late.iloc[-1]["Close"] / late.iloc[0]["Open"] - 1.0)
        session_vwap = float(day["Amount"].sum() / day["Volume"].sum())
        close = float(day.iloc[-1]["Close"])
        late_volume_share = float(late["Volume"].sum() / day["Volume"].sum())
        selloff = max(-late_return, 0.0)
        rows.append(
            {
                "trade_date": trade_date,
                "late_return": late_return,
                "previous_hour_return": previous_return,
                "late_efficiency": _efficiency(late),
                "late_volume_share": late_volume_share,
                "session_vwap": session_vwap,
                "close": close,
                "LATE_PATH_SELLING_score": selloff * _efficiency(late),
                "CLOSE_VWAP_DISLOCATION_score": max(session_vwap / close - 1.0, 0.0),
                "LATE_VOLUME_PRESSURE_score": selloff * late_volume_share,
                "LATE_ACCELERATION_score": selloff * max(previous_return - late_return, 0.0),
            }
        )
    return pd.DataFrame(rows).set_index("trade_date")


def _cooldown(raw_dates: pd.DatetimeIndex, calendar: pd.DatetimeIndex, sessions: int) -> pd.DatetimeIndex:
    positions = {value: index for index, value in enumerate(calendar)}
    kept: list[pd.Timestamp] = []
    last = -10**9
    for value in raw_dates.sort_values():
        position = positions[value]
        if position - last > sessions:
            kept.append(value)
            last = position
    return pd.DatetimeIndex(kept, name="trade_date")


def _run(protocol: dict[str, object], bars: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    features = _features(bars)
    threshold_spec = protocol["threshold"]
    density_spec = protocol["density"]
    calendar = pd.DatetimeIndex(features.index, name="trade_date")
    density_calendar = calendar[calendar >= pd.Timestamp(protocol["dataset"]["evaluation_start"])]
    events: list[pd.DataFrame] = []
    rows: list[dict[str, object]] = []
    for mechanism in map(str, protocol["mechanisms"]):
        score = features[f"{mechanism}_score"]
        threshold = score.shift(int(threshold_spec["lag_sessions"])).rolling(
            int(threshold_spec["lookback_sessions"]), min_periods=int(threshold_spec["lookback_sessions"])
        ).quantile(float(threshold_spec["quantile"]))
        raw = threshold.notna() & score.gt(0.0) & score.ge(threshold)
        raw_dates = pd.DatetimeIndex(features.index[raw & (features.index >= density_calendar[0])])
        kept = _cooldown(raw_dates, calendar, int(density_spec["cooldown_sessions"]))
        selected = features.loc[kept].copy().reset_index()
        selected.insert(0, "mechanism_id", mechanism)
        selected["score"] = score.reindex(kept).to_numpy()
        selected["threshold"] = threshold.reindex(kept).to_numpy()
        selected["signal_time"] = pd.to_datetime(selected["trade_date"]) + pd.Timedelta(hours=15)
        events.append(selected)
        flags = pd.Series(0, index=density_calendar, dtype="int64")
        flags.loc[flags.index.intersection(kept)] = 1
        rolling = flags.rolling(int(density_spec["window_sessions"]), min_periods=int(density_spec["window_sessions"])).sum().dropna()
        median = float(rolling.median())
        p10 = float(rolling.quantile(0.10, interpolation="lower"))
        passed = bool(
            int(density_spec["median_min"]) <= median <= int(density_spec["median_max"])
            and p10 >= int(density_spec["p10_min"])
        )
        rows.append(
            {
                "mechanism_id": mechanism,
                "raw_events": int(len(raw_dates)),
                "independent_events": int(len(kept)),
                "rolling_60_median": median,
                "rolling_60_p10": p10,
                "density_pass": passed,
                "evidence": "DENSITY_CAPABLE" if passed else "EVIDENCE_RATE_FAIL",
            }
        )
        features[f"{mechanism}_threshold"] = threshold
        features[f"{mechanism}_raw_event"] = raw
    return features.reset_index(), pd.concat(events, ignore_index=True), pd.DataFrame(rows)


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("reads_post_event_prices"):
        raise ValueError("density census may not read post-event prices")
    if any(bool(protocol.get(key)) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("density census may not mutate lifecycle state")
    if len(protocol["mechanisms"]) != int(protocol["registered_trials"]):
        raise ValueError("mechanism count differs from registered trials")

    target = protocol["research_target"]
    loaded = load_intraday_research_data(repo / str(protocol["dataset"]["directory"]), str(target["symbol"]))
    features, events, density = _run(protocol, loaded.frames["1m"])
    boundary = pd.Timestamp("2026-01-05")
    mutated = loaded.frames["1m"].copy()
    mask = pd.to_datetime(mutated["Date"]).dt.normalize() >= boundary
    mutated.loc[mask, ["Open", "High", "Low", "Close"]] *= 1.1
    mutated_features, mutated_events, _ = _run(protocol, mutated)
    pd.testing.assert_frame_equal(
        features.loc[features["trade_date"] < boundary].reset_index(drop=True),
        mutated_features.loc[mutated_features["trade_date"] < boundary].reset_index(drop=True),
    )
    pd.testing.assert_frame_equal(
        events.loc[events["trade_date"] < boundary].reset_index(drop=True),
        mutated_events.loc[mutated_events["trade_date"] < boundary].reset_index(drop=True),
    )

    features.to_csv(artifacts / "proxy_features.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    events.to_csv(artifacts / "proxy_events.csv.gz", index=False, encoding="utf-8-sig", compression={"method": "gzip", "compresslevel": 9, "mtime": 0}, lineterminator="\n")
    density.to_csv(artifacts / "density_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    capable = density.loc[density["density_pass"]]
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "causal_prefix_mutation_audit": "PASS",
        "registered_trials": int(protocol["registered_trials"]),
        "density_capable": capable["mechanism_id"].tolist(),
        "route_decision": "CONTINUE_DIRECTION_TEST" if not capable.empty else "STOP_PROXY_FAMILY",
        "candidate_created": False,
    }
    _write(artifacts / "density_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n状态：COMPLETE。四个代理完成因果性与滚动事件密度审计。\n",
        encoding="utf-8",
    )
    lines = ["# 20260912_S004_EX08 结论", "", "|代理|独立事件|60日中位数|P10|标签|", "|---|---:|---:|---:|---|"]
    for row in density.itertuples(index=False):
        lines.append(f"|{row.mechanism_id}|{row.independent_events}|{row.rolling_60_median:.0f}|{row.rolling_60_p10:.0f}|{row.evidence}|")
    lines.extend(["", f"下一步：`{summary['route_decision']}`。本轮没有读取事件后收益或创建候选。", ""])
    (experiment / "04_conclusion.md").write_text("\n".join(lines), encoding="utf-8")
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": target["strategy_id"],
            "symbol": target["symbol"],
            "development_cutoff": protocol["dataset"]["cutoff"],
            "promotion_allowed": False,
        },
    )


if __name__ == "__main__":
    main()
