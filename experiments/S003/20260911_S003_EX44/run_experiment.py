from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260911_S003_EX44"


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
    forbidden = (
        "conditional_return_analysis",
        "parameter_selection",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX44 may only generate return-free moneyflow breadth events")

    dataset = protocol["dataset"]
    source = repo_root / "experiments" / dataset["source_experiment"]
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": dataset["source_manifest_sha256"],
        source / "artifacts" / "constituent_moneyflow_panel.csv.gz": dataset[
            "source_panel_sha256"
        ],
        source / "artifacts" / "data_quality.json": dataset["source_quality_sha256"],
        repo_root / dataset["calendar_manifest"]: dataset["calendar_manifest_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    if not _read_json(source / "artifacts" / "data_quality.json").get("passed"):
        raise ValueError("EX43 constituent moneyflow data gate did not pass")

    panel = pd.read_csv(source / "artifacts" / "constituent_moneyflow_panel.csv.gz")
    panel["dt"] = pd.to_datetime(panel["dt"]).dt.normalize()
    panel["weight"] = pd.to_numeric(panel["weight"], errors="raise")
    panel["net_mf_amount"] = pd.to_numeric(panel["net_mf_amount"], errors="coerce")
    panel["observed_weight"] = panel["weight"].where(panel["observed_moneyflow"].astype(bool), 0.0)
    panel["positive_weight"] = panel["weight"].where(panel["net_mf_amount"].gt(0), 0.0)
    daily = panel.groupby("dt", sort=True, observed=True).agg(
        total_weight=("weight", "sum"),
        observed_weight=("observed_weight", "sum"),
        positive_weight=("positive_weight", "sum"),
        member_count=("con_code", "count"),
        observed_member_count=("observed_moneyflow", "sum"),
    )
    daily["observed_weight_ratio"] = daily["observed_weight"] / daily["total_weight"]
    daily["moneyflow_breadth"] = daily["positive_weight"] / daily["observed_weight"]
    spec = protocol["feature"]
    daily.loc[
        daily["observed_weight_ratio"].lt(float(spec["minimum_observed_weight_ratio"])),
        "moneyflow_breadth",
    ] = pd.NA
    lookback = int(spec["threshold_lookback_valid_sessions"])
    daily["prior_60_breadth_q80"] = (
        daily["moneyflow_breadth"]
        .shift(1)
        .rolling(lookback, min_periods=lookback)
        .quantile(float(spec["threshold_quantile"]))
    )

    market = load_market_data(repo_root / "data" / "raw", "510500.SH").daily
    calendar = pd.DatetimeIndex(pd.to_datetime(market["dt"]).dt.normalize())
    calendar = calendar[calendar <= pd.Timestamp(dataset["development_cutoff"])]
    next_session = pd.Series(calendar[1:], index=calendar[:-1])
    selected = daily.loc[
        daily["moneyflow_breadth"].ge(daily["prior_60_breadth_q80"])
    ].copy()
    selected["event_date"] = selected.index.map(next_session)
    selected = selected.loc[selected["event_date"].notna()].copy()
    events = selected.reset_index(names="signal_date")
    events.insert(0, "mechanism", protocol["research_target"]["mechanism"])
    events["signal_clock"] = spec["signal_time"]
    events["execution_clock"] = spec["entry_time"]
    events = events[
        [
            "mechanism",
            "signal_date",
            "event_date",
            "signal_clock",
            "execution_clock",
            "moneyflow_breadth",
            "prior_60_breadth_q80",
            "observed_weight_ratio",
        ]
    ].sort_values("event_date")
    if events.duplicated("event_date").any():
        raise ValueError("event census generated more than one event per day")

    indicator = pd.Series(0, index=calendar, dtype=int)
    indicator.loc[pd.DatetimeIndex(events["event_date"])] = 1
    rolling_sessions = int(protocol["density_gate"]["rolling_sessions"])
    rolling = indicator.rolling(rolling_sessions, min_periods=rolling_sessions).sum()
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
        float(gate["median_min"])
        <= density["rolling_60_median"]
        <= float(gate["median_max"])
        and density["rolling_60_p10"] >= float(gate["p10_min"])
    )

    feature_output = daily.reset_index()
    for frame, columns in (
        (events, ["signal_date", "event_date"]),
        (feature_output, ["dt"]),
    ):
        for column in columns:
            frame[column] = pd.to_datetime(frame[column]).dt.strftime("%Y-%m-%d")
    events.to_csv(artifacts / "mechanism_events.csv", index=False, lineterminator="\n")
    feature_output.to_csv(
        artifacts / "moneyflow_breadth_features.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    pd.DataFrame([density]).to_csv(
        artifacts / "density_metrics.csv", index=False, lineterminator="\n"
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
        "# S003 EX44 执行\n\n"
        f"成分资金流宽度延续事件密度门：`{status}`。共产生{density['event_count']}个事件；"
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
        "# S003 EX44 结论\n\n"
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
