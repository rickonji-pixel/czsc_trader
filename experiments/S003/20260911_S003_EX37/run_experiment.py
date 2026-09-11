from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260911_S003_EX37"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo_root = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        protocol.get(key)
        for key in (
            "conditional_return_analysis",
            "parameter_selection",
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX37 may only generate return-free margin-flow events")

    dataset = protocol["dataset"]
    source = repo_root / "experiments" / dataset["source_experiment"]
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": dataset["source_manifest_sha256"],
        source / "artifacts" / "etf_margin_flow_panel.csv.gz": dataset["source_panel_sha256"],
        source / "artifacts" / "data_quality.json": dataset["source_quality_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    quality = _read_json(source / "artifacts" / "data_quality.json")
    if not quality["passed"]:
        raise ValueError("EX36 margin-flow data gate did not pass")

    panel = pd.read_csv(source / "artifacts" / "etf_margin_flow_panel.csv.gz")
    panel["dt"] = pd.to_datetime(panel["dt"]).dt.normalize()
    panel["available_for_strategy_from"] = pd.to_datetime(
        panel["available_for_strategy_from"]
    ).dt.normalize()
    panel = panel.sort_values("dt").reset_index(drop=True)
    feature = protocol["feature"]
    lookback = int(feature["threshold_lookback"])
    quantile = float(feature["threshold_quantile"])
    panel["net_financing_flow"] = panel["rzmre"] - panel["rzche"]
    panel["net_financing_flow_rate"] = panel["net_financing_flow"].div(
        panel["rzye"].shift(1)
    )
    panel["prior_lower_quintile"] = (
        panel["net_financing_flow_rate"].shift(1).rolling(lookback, min_periods=lookback).quantile(quantile)
    )
    panel["event"] = panel["net_financing_flow_rate"].le(panel["prior_lower_quintile"])
    events = panel.loc[panel["event"] & panel["available_for_strategy_from"].notna()].copy()
    events = events.rename(columns={"dt": "signal_date", "available_for_strategy_from": "event_date"})
    events.insert(0, "mechanism", protocol["research_target"]["mechanism"])
    events["signal_clock"] = feature["signal_time"]
    events["execution_clock"] = "NEXT_OPEN"
    event_columns = [
        "mechanism",
        "signal_date",
        "event_date",
        "signal_clock",
        "execution_clock",
        "net_financing_flow",
        "net_financing_flow_rate",
        "prior_lower_quintile",
    ]
    events = events[event_columns].sort_values("event_date").reset_index(drop=True)

    calendar = pd.DatetimeIndex(panel["dt"])
    event_indicator = pd.Series(0, index=calendar, dtype=int)
    signal_dates = pd.DatetimeIndex(events["signal_date"])
    event_indicator.loc[event_indicator.index.intersection(signal_dates)] = 1
    rolling = event_indicator.rolling(
        int(protocol["density_gate"]["rolling_sessions"]),
        min_periods=int(protocol["density_gate"]["rolling_sessions"]),
    ).sum()
    rolling = rolling.loc[rolling.index >= pd.Timestamp(dataset["density_evaluation_start"])]
    if rolling.empty:
        raise ValueError("no full density evaluation windows")
    density = {
        "mechanism": protocol["research_target"]["mechanism"],
        "event_count": int(len(events)),
        "rolling_60_median": float(rolling.median()),
        "rolling_60_p10": float(rolling.quantile(0.10)),
        "rolling_60_min": float(rolling.min()),
        "rolling_60_max": float(rolling.max()),
    }
    gate = protocol["density_gate"]
    density["density_eligible"] = bool(
        float(gate["median_min"]) <= density["rolling_60_median"] <= float(gate["median_max"])
        and density["rolling_60_p10"] >= float(gate["p10_min"])
    )
    density_frame = pd.DataFrame([density])
    feature_columns = [
        "dt",
        "rzye",
        "rzmre",
        "rzche",
        "net_financing_flow",
        "net_financing_flow_rate",
        "prior_lower_quintile",
        "event",
        "available_for_strategy_from",
    ]
    features = panel[feature_columns].copy()
    for frame, columns in ((events, ["signal_date", "event_date"]), (features, ["dt", "available_for_strategy_from"])):
        for column in columns:
            frame[column] = pd.to_datetime(frame[column]).dt.strftime("%Y-%m-%d")
    events.to_csv(artifacts / "mechanism_events.csv", index=False, lineterminator="\n")
    density_frame.to_csv(artifacts / "density_metrics.csv", index=False, lineterminator="\n")
    features.to_csv(
        artifacts / "margin_flow_features.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "density_pass": density["density_eligible"],
        "event_count": density["event_count"],
        "conditional_return_analysis": False,
        "next_step": "RETURN_EVALUATION" if density["density_eligible"] else "STOP_MECHANISM",
    }
    _write_json(artifacts / "census_summary.json", summary)
    status = "PASS" if density["density_eligible"] else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S003 EX37 执行\n\n"
        f"融资去杠杆事件密度门：`{status}`。共产生{density['event_count']}个事件，"
        f"滚动60日中位数{density['rolling_60_median']:.1f}，第10百分位"
        f"{density['rolling_60_p10']:.1f}，最小值{density['rolling_60_min']:.1f}，"
        f"最大值{density['rolling_60_max']:.1f}。\n\n本轮没有读取510500事件后收益。\n",
        encoding="utf-8",
    )
    conclusion = (
        "机制获得下一轮收益评价资格。"
        if density["density_eligible"]
        else "机制未达到三个月观察目标所需密度，停止该固定定义。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX37 结论\n\n"
        f"结论：`{status}`。{conclusion}本轮没有创建候选或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": "510500.SH",
            "development_cutoff": dataset["development_cutoff"],
            "status": status,
            "density_pass": density["density_eligible"],
            "conditional_return_analysis": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
