from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX03"


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _load_daily_from_minutes(repo: Path, symbol: str, cutoff: pd.Timestamp) -> pd.DataFrame:
    code = symbol.split(".", maxsplit=1)[0]
    paths = sorted((repo / "data/raw").glob(f"{code}_1m_*.csv"))
    if not paths:
        raise FileNotFoundError(f"no 1m files for {symbol}")
    frames = [pd.read_csv(path, parse_dates=["datetime"]) for path in paths]
    minutes = pd.concat(frames, ignore_index=True).sort_values("datetime")
    minutes = minutes.loc[minutes["datetime"].dt.normalize() <= cutoff].copy()
    if minutes["datetime"].duplicated().any():
        raise ValueError(f"duplicate minute bars for {symbol}")
    minutes["date"] = minutes["datetime"].dt.normalize()
    counts = minutes.groupby("date").size()
    if not counts.eq(240).all():
        raise ValueError(f"incomplete sessions for {symbol}: {counts.loc[~counts.eq(240)].to_dict()}")
    grouped = minutes.groupby("date", sort=True)
    daily = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "volume": grouped["volume"].sum(),
        }
    )
    daily["return"] = daily["close"].pct_change()
    daily["close_location"] = (daily["close"] - daily["low"]) / (daily["high"] - daily["low"]).replace(0.0, np.nan)
    return daily


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
    validate_experiment_archive(repo / "experiments/S005/20260913_S005_EX02")

    cutoff = pd.Timestamp(str(protocol["development_cutoff"]))
    start = pd.Timestamp(str(protocol["development_start"]))
    primary = _load_daily_from_minutes(repo, str(protocol["symbol"]), cutoff)
    context = _load_daily_from_minutes(repo, str(protocol["context_symbol"]), cutoff)
    frame = primary.join(context[["return"]].rename(columns={"return": "context_return"}), how="inner")
    frame["relative_return"] = frame["return"] - frame["context_return"]
    reference = int(protocol["threshold_reference_sessions"])
    quantiles = [float(value) for value in protocol["threshold_quantiles"]]
    for quantile in quantiles:
        frame[f"threshold_q{int(quantile * 100)}"] = frame["relative_return"].shift(1).rolling(reference).quantile(quantile)
    frame = frame.loc[(frame.index >= start) & (frame.index <= cutoff)].copy()

    density_rule = protocol["density"]
    cooldown = int(density_rule["cooldown_sessions"])
    window = int(density_rule["rolling_window_sessions"])
    minimum_median = float(density_rule["minimum_median"])
    minimum_p10 = float(density_rule["minimum_p10"])
    ledger = frame[["return", "context_return", "relative_return", "close_location"]].copy()
    summaries: list[dict[str, object]] = []
    for quantile in quantiles:
        name = f"RELATIVE_FLOW_Q{int(quantile * 100)}"
        threshold = frame[f"threshold_q{int(quantile * 100)}"]
        raw = (frame["relative_return"] >= threshold) & (frame["close_location"] >= float(protocol["minimum_close_location"]))
        accepted = _cooldown(raw, cooldown)
        ledger[f"{name}__threshold"] = threshold
        ledger[f"{name}__raw"] = raw.fillna(False).astype(int)
        ledger[f"{name}__accepted"] = accepted.astype(int)
        summary = {"mechanism": name, "quantile": quantile, "raw_events": int(raw.fillna(False).sum()), **_density(accepted, window)}
        summary["density_pass"] = bool(summary["rolling_60_median"] >= minimum_median and summary["rolling_60_p10"] >= minimum_p10)
        summary["yearly_events"] = {str(year): int(accepted.loc[accepted.index.year == year].sum()) for year in sorted(accepted.index.year.unique())}
        summaries.append(summary)

    summary_frame = pd.DataFrame(summaries).sort_values("quantile")
    passing = summary_frame.loc[summary_frame["density_pass"]].sort_values("quantile", ascending=False)
    selected = None if passing.empty else str(passing.iloc[0]["mechanism"])
    decision = "PROCEED_TO_FIXED_RETURN_FALSIFICATION" if selected else "STOP_RELATIVE_FLOW_FAMILY_ON_DENSITY"
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "reads_post_event_prices": False,
        "session_count": int(len(frame)),
        "tested_path_count": len(quantiles),
        "selection_rule": protocol["selection_rule"],
        "selected_mechanism": selected,
        "decision": decision,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": raw_file_sha256(protocol_path),
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    ledger.reset_index(names="date").to_csv(artifacts / "event_ledger.csv.gz", index=False, compression=compression, lineterminator="\n")
    summary_frame.to_json(artifacts / "density_summary.json", orient="records", force_ascii=False, indent=2)
    _write_json(artifacts / "selection_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S005 EX03 执行\n\n状态：COMPLETE。三个预注册相对轮动强度已完成无收益密度筛选。\n",
        encoding="utf-8",
    )
    lines = ["# S005 EX03 结论", "", "|路径|独立事件|60日中位数|P10|密度门|", "|---|---:|---:|---:|---|"]
    for item in summaries:
        lines.append(
            f"|{item['mechanism']}|{item['events']}|{item['rolling_60_median']:.1f}|"
            f"{item['rolling_60_p10']:.1f}|{'PASS' if item['density_pass'] else 'FAIL'}|"
        )
    lines.extend(["", f"按预注册规则冻结：`{selected or 'NONE'}`。", f"裁决：`{decision}`。本轮不读取未来收益，不生成候选。", ""])
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
            "selected_mechanism": selected,
            "candidate_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
