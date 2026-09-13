from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256

EXPERIMENT_ID = "20260913_S005_EX10"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _daily(repo: Path, cutoff: pd.Timestamp) -> pd.DataFrame:
    parts = [pd.read_csv(path, parse_dates=["datetime"]) for path in sorted((repo / "data/raw").glob("588080_1m_*.csv"))]
    data = pd.concat(parts, ignore_index=True).sort_values("datetime")
    data = data.loc[data["datetime"].dt.normalize() <= cutoff].copy()
    data["date"] = data["datetime"].dt.normalize()
    if not data.groupby("date").size().eq(240).all():
        raise ValueError("incomplete sessions")
    grouped = data.groupby("date", sort=True)
    return pd.DataFrame({"open": grouped.open.first(), "high": grouped.high.max(), "low": grouped.low.min(), "close": grouped.close.last()})


def _cooldown(events: pd.Series, sessions: int) -> pd.Series:
    accepted = pd.Series(False, index=events.index)
    last = -sessions - 1
    for position, active in enumerate(events.fillna(False).to_numpy(dtype=bool)):
        if active and position - last > sessions:
            accepted.iloc[position] = True
            last = position
    return accepted


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    validate_experiment_archive(repo / "experiments/S005/20260913_S005_EX09")
    cutoff = pd.Timestamp(str(protocol["development_cutoff"]))
    frame = _daily(repo, cutoff)
    previous_close = frame.close.shift(1)
    frame["return"] = frame.close / previous_close - 1.0
    frame["true_range_pct"] = pd.concat([(frame.high - frame.low), (frame.high - previous_close).abs(), (frame.low - previous_close).abs()], axis=1).max(axis=1) / previous_close
    frame["close_location"] = (frame.close - frame.low) / (frame.high - frame.low).replace(0.0, np.nan)
    reference = int(protocol["threshold_reference_sessions"])
    quantiles = [float(value) for value in protocol["threshold_quantiles"]]
    for quantile in quantiles:
        frame[f"q{int(quantile * 100)}"] = frame.true_range_pct.shift(1).rolling(reference).quantile(quantile)
    frame = frame.loc[(frame.index >= pd.Timestamp(str(protocol["development_start"]))) & (frame.index <= cutoff)].copy()
    density = protocol["density"]
    ledger = frame[["return", "true_range_pct", "close_location"]].copy()
    summaries = []
    for quantile in quantiles:
        name = f"VOLATILITY_IGNITION_Q{int(quantile * 100)}"
        threshold = frame[f"q{int(quantile * 100)}"]
        raw = (frame.true_range_pct >= threshold) & (frame["return"] > 0) & (frame.close_location >= float(protocol["minimum_close_location"]))
        accepted = _cooldown(raw, int(density["cooldown_sessions"]))
        rolling = accepted.astype(int).rolling(int(density["rolling_window_sessions"]), min_periods=int(density["rolling_window_sessions"])).sum().dropna()
        item = {"mechanism": name, "quantile": quantile, "raw_events": int(raw.sum()), "independent_events": int(accepted.sum()), "rolling_60_median": float(rolling.median()), "rolling_60_p10": float(rolling.quantile(0.1)), "rolling_60_minimum": float(rolling.min()), "rolling_60_maximum": float(rolling.max())}
        item["density_pass"] = bool(item["rolling_60_median"] >= float(density["minimum_median"]) and item["rolling_60_p10"] >= float(density["minimum_p10"]))
        summaries.append(item)
        ledger[f"{name}__threshold"] = threshold
        ledger[f"{name}__accepted"] = accepted.astype(int)
    summary = pd.DataFrame(summaries)
    passing = summary.loc[summary.density_pass].sort_values("quantile", ascending=False)
    selected = None if passing.empty else str(passing.iloc[0].mechanism)
    decision = "PROCEED_TO_FIXED_THREE_SESSION_RETURN_TEST" if selected else "STOP_VOLATILITY_IGNITION_ON_DENSITY"
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    ledger.reset_index(names="date").to_csv(artifacts / "event_ledger.csv.gz", index=False, compression=compression, lineterminator="\n")
    summary.to_json(artifacts / "density_summary.json", orient="records", force_ascii=False, indent=2)
    evidence = {"schema_version": 1, "experiment_id": EXPERIMENT_ID, "status": "COMPLETE", "reads_post_event_prices": False, "selected_mechanism": selected, "decision": decision, "protocol_sha256": raw_file_sha256(protocol_path), "candidate_created": False, "strategy_frozen": False, "pte_mutated": False}
    (artifacts / "selection_evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# S005 EX10 结论", "", "|路径|独立事件|60日中位数|P10|密度门|", "|---|---:|---:|---:|---|"]
    lines += [f"|{x['mechanism']}|{x['independent_events']}|{x['rolling_60_median']:.1f}|{x['rolling_60_p10']:.1f}|{'PASS' if x['density_pass'] else 'FAIL'}|" for x in summaries]
    lines += ["", f"冻结路径：`{selected or 'NONE'}`；裁决：`{decision}`。本轮未读取未来收益。", ""]
    (experiment / "03_execution.md").write_text("# S005 EX10 执行\n\n状态：COMPLETE。已完成无收益密度筛选。\n", encoding="utf-8")
    (experiment / "04_conclusion.md").write_text("\n".join(lines), encoding="utf-8")
    build_experiment_manifest(experiment, {"experiment_id": EXPERIMENT_ID, "status": "COMPLETE", "experiment_type": protocol["experiment_type"], "strategy_id": "S005", "symbol": protocol["symbol"], "development_cutoff": protocol["development_cutoff"], "decision": decision, "selected_mechanism": selected, "candidate_created": False, "strategy_frozen": False, "pte_mutated": False})
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
