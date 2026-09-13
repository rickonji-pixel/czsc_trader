from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX02"


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _load_minutes(repo: Path, symbol: str, cutoff: pd.Timestamp) -> pd.DataFrame:
    code = symbol.split(".", maxsplit=1)[0]
    frames = [pd.read_csv(path, parse_dates=["datetime"]) for path in sorted((repo / "data/raw").glob(f"{code}_1m_*.csv"))]
    if not frames:
        raise FileNotFoundError(f"no 1m files for {symbol}")
    frame = pd.concat(frames, ignore_index=True).sort_values("datetime")
    frame = frame.loc[frame["datetime"].dt.normalize() <= cutoff].copy()
    if frame["datetime"].duplicated().any():
        raise ValueError(f"duplicate minute bars for {symbol}")
    return frame


def _last_at_or_before(group: pd.DataFrame, clock: str) -> float:
    selected = group.loc[group["clock"] <= clock, "close"]
    return float(selected.iloc[-1]) if not selected.empty else np.nan


def _session_features(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    data["date"] = data["datetime"].dt.normalize()
    data["clock"] = data["datetime"].dt.strftime("%H:%M")
    rows: list[dict[str, float | pd.Timestamp]] = []
    for date, group in data.groupby("date", sort=True):
        group = group.sort_values("datetime")
        if len(group) != 240:
            raise ValueError(f"incomplete 1m session: {date.date()} has {len(group)} rows")
        w1000 = group.loc[group["clock"] <= "10:00"]
        w1030 = group.loc[group["clock"] <= "10:30"]
        morning = group.loc[group["clock"] <= "11:30"]
        w1330 = group.loc[group["clock"] <= "13:30"]
        open_price = float(group["open"].iloc[0])
        weighted_price = float((w1330["close"] * w1330["volume"]).sum() / w1330["volume"].sum())
        rows.append(
            {
                "date": date,
                "open": open_price,
                "high": float(group["high"].max()),
                "low": float(group["low"].min()),
                "close": float(group["close"].iloc[-1]),
                "volume": float(group["volume"].sum()),
                "close_1000": float(w1000["close"].iloc[-1]),
                "low_1000": float(w1000["low"].min()),
                "high_1030": float(w1030["high"].max()),
                "low_1030": float(w1030["low"].min()),
                "close_1030": float(w1030["close"].iloc[-1]),
                "volume_1000": float(w1000["volume"].sum()),
                "volume_1030": float(w1030["volume"].sum()),
                "morning_high": float(morning["high"].max()),
                "morning_close": float(morning["close"].iloc[-1]),
                "morning_volume": float(morning["volume"].sum()),
                "close_1315": _last_at_or_before(group, "13:15"),
                "close_1330": float(w1330["close"].iloc[-1]),
                "vwap_1330": weighted_price,
            }
        )
    result = pd.DataFrame(rows).set_index("date").sort_index()
    previous_close = result["close"].shift(1)
    result["previous_close"] = previous_close
    result["gap"] = result["open"] / previous_close - 1.0
    result["return_1000"] = result["close_1000"] / result["open"] - 1.0
    result["return_1030"] = result["close_1030"] / result["open"] - 1.0
    result["morning_return"] = result["morning_close"] / result["open"] - 1.0
    result["return_1330"] = result["close_1330"] / result["open"] - 1.0
    result["close_location_1030"] = (result["close_1030"] - result["low_1030"]) / (result["high_1030"] - result["low_1030"]).replace(0.0, np.nan)
    result["close_location"] = (result["close"] - result["low"]) / (result["high"] - result["low"]).replace(0.0, np.nan)
    result["volume_1000_ratio"] = result["volume_1000"] / result["volume_1000"].shift(1).rolling(20).median()
    result["volume_1030_ratio"] = result["volume_1030"] / result["volume_1030"].shift(1).rolling(20).median()
    result["morning_volume_ratio"] = result["morning_volume"] / result["morning_volume"].shift(1).rolling(20).median()
    result["daily_volume_ratio"] = result["volume"] / result["volume"].shift(1).rolling(20).median()
    previous_high = result["high"].shift(1)
    previous_low = result["low"].shift(1)
    true_range = pd.concat(
        [result["high"] - result["low"], (result["high"] - previous_close).abs(), (result["low"] - previous_close).abs()], axis=1
    ).max(axis=1)
    result["prior_atr5_pct"] = true_range.shift(1).rolling(5).mean() / previous_close
    result["compression_cutoff"] = result["prior_atr5_pct"].shift(1).rolling(60).quantile(0.35)
    result["prior_high_10"] = previous_high.rolling(10).max()
    result["range_pct"] = (result["high"] - result["low"]) / previous_close
    result["previous_low"] = previous_low
    return result


def _cooldown(events: pd.Series, sessions: int) -> pd.Series:
    accepted = pd.Series(False, index=events.index)
    last_position = -sessions - 1
    for position, active in enumerate(events.fillna(False).astype(bool).to_numpy()):
        if active and position - last_position > sessions:
            accepted.iloc[position] = True
            last_position = position
    return accepted


def _density(events: pd.Series, window: int) -> dict[str, float | int]:
    counts = events.astype(int).rolling(window, min_periods=window).sum().dropna()
    return {
        "events": int(events.sum()),
        "rolling_60_median": float(counts.median()),
        "rolling_60_p10": float(counts.quantile(0.10)),
        "rolling_60_minimum": float(counts.min()),
        "rolling_60_maximum": float(counts.max()),
    }


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read_json(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    validate_experiment_archive(repo / "experiments/S005/20260913_S005_EX01")

    cutoff = pd.Timestamp(str(protocol["development_cutoff"]))
    start = pd.Timestamp(str(protocol["development_start"]))
    primary = _session_features(_load_minutes(repo, str(protocol["symbol"]), cutoff))
    context = _session_features(_load_minutes(repo, str(protocol["context_symbol"]), cutoff))
    frame = primary.join(context[["return_1030"]].rename(columns={"return_1030": "context_return_1030"}), how="inner")
    frame = frame.loc[(frame.index >= start) & (frame.index <= cutoff)].copy()

    mechanisms = protocol["mechanisms"]
    gap = mechanisms["GAP_ACCEPTANCE_1000"]
    relative = mechanisms["OPENING_RELATIVE_LEAD_1030"]
    pullback = mechanisms["IMPULSE_PULLBACK_RESUME_1330"]
    compression = mechanisms["COMPRESSION_BREAKOUT_CLOSE"]
    impulse = (frame["morning_high"] - frame["open"]).replace(0.0, np.nan)
    retracement = (frame["morning_high"] - frame["close_1330"]) / impulse

    raw_events = {
        "GAP_ACCEPTANCE_1000": (frame["gap"] >= float(gap["minimum_gap"]))
        & (frame["return_1000"] >= float(gap["minimum_opening_return"]))
        & (frame["low_1000"] / frame["previous_close"] - 1.0 >= float(gap["minimum_opening_low_to_previous_close"]))
        & (frame["volume_1000_ratio"] >= float(gap["minimum_opening_volume_ratio"])),
        "OPENING_RELATIVE_LEAD_1030": (frame["return_1030"] >= float(relative["minimum_return"]))
        & (frame["return_1030"] - frame["context_return_1030"] >= float(relative["minimum_relative_return"]))
        & (frame["close_location_1030"] >= float(relative["minimum_close_location"]))
        & (frame["volume_1030_ratio"] >= float(relative["minimum_opening_volume_ratio"])),
        "IMPULSE_PULLBACK_RESUME_1330": (frame["morning_return"] >= float(pullback["minimum_morning_return"]))
        & (retracement >= 0.0)
        & (retracement <= float(pullback["maximum_retracement"]))
        & (frame["return_1330"] >= float(pullback["minimum_decision_return"]))
        & (frame["morning_volume_ratio"] >= float(pullback["minimum_morning_volume_ratio"]))
        & (frame["close_1330"] >= frame["vwap_1330"])
        & (frame["close_1330"] > frame["close_1315"]),
        "COMPRESSION_BREAKOUT_CLOSE": (frame["prior_atr5_pct"] <= frame["compression_cutoff"])
        & (frame["close"] > frame["prior_high_10"])
        & (frame["range_pct"] >= float(compression["minimum_range_expansion"]) * frame["prior_atr5_pct"])
        & (frame["close_location"] >= float(compression["minimum_close_location"]))
        & (frame["daily_volume_ratio"] >= float(compression["minimum_daily_volume_ratio"])),
    }

    density_rule = protocol["density"]
    cooldown = int(density_rule["cooldown_sessions"])
    window = int(density_rule["rolling_window_sessions"])
    minimum_median = float(density_rule["minimum_median"])
    minimum_p10 = float(density_rule["minimum_p10"])
    event_frame = pd.DataFrame(index=frame.index)
    summaries: list[dict[str, object]] = []
    for mechanism, raw in raw_events.items():
        accepted = _cooldown(raw, cooldown)
        event_frame[f"{mechanism}__raw"] = raw.fillna(False).astype(int)
        event_frame[f"{mechanism}__accepted"] = accepted.astype(int)
        summary = {"mechanism": mechanism, "raw_events": int(raw.fillna(False).sum()), **_density(accepted, window)}
        summary["density_pass"] = bool(summary["rolling_60_median"] >= minimum_median and summary["rolling_60_p10"] >= minimum_p10)
        summary["yearly_events"] = {str(year): int(accepted.loc[accepted.index.year == year].sum()) for year in sorted(accepted.index.year.unique())}
        summaries.append(summary)

    summary_frame = pd.DataFrame(summaries)
    passing = summary_frame.loc[summary_frame["density_pass"], "mechanism"].tolist()
    decision = "PROCEED_TO_FIXED_RETURN_FALSIFICATION" if passing else "STOP_PREREGISTERED_UPSIDE_FAMILY_ON_DENSITY"
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "reads_post_event_prices": False,
        "session_count": int(len(frame)),
        "mechanism_count": len(raw_events),
        "passing_mechanisms": passing,
        "decision": decision,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": raw_file_sha256(protocol_path),
    }
    compression_options = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    event_frame.reset_index(names="date").to_csv(
        artifacts / "event_ledger.csv.gz", index=False, compression=compression_options, lineterminator="\n"
    )
    summary_frame.to_json(artifacts / "density_summary.json", orient="records", force_ascii=False, indent=2)
    _write_json(artifacts / "census_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S005 EX02 执行\n\n状态：COMPLETE。四个预注册机制已完成无收益密度筛选；事件表不含任何未来收益字段。\n",
        encoding="utf-8",
    )
    lines = ["# S005 EX02 结论", "", "|机制|原始事件|独立事件|60日中位数|P10|密度门|", "|---|---:|---:|---:|---:|---|"]
    for item in summaries:
        lines.append(
            f"|{item['mechanism']}|{item['raw_events']}|{item['events']}|{item['rolling_60_median']:.1f}|"
            f"{item['rolling_60_p10']:.1f}|{'PASS' if item['density_pass'] else 'FAIL'}|"
        )
    lines.extend(["", f"裁决：`{decision}`。本轮不生成候选，不修改 SM 或 PTE。", ""])
    (experiment / "04_conclusion.md").write_text("\n".join(lines), encoding="utf-8")
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S005",
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": decision,
            "passing_mechanism_count": len(passing),
            "candidate_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
