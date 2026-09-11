from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260911_S003_EX42"


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
        raise ValueError("EX42 may only generate return-free ETF discount events")

    dataset = protocol["dataset"]
    source = repo_root / "experiments" / dataset["source_experiment"]
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": dataset["source_manifest_sha256"],
        source / "artifacts" / "governed_etf_share_nav_panel.csv.gz": dataset[
            "source_panel_sha256"
        ],
        source / "artifacts" / "data_quality.json": dataset["source_quality_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    if not _read_json(source / "artifacts" / "data_quality.json")["passed"]:
        raise ValueError("EX40 corporate-action-aware data gate did not pass")

    panel = pd.read_csv(source / "artifacts" / "governed_etf_share_nav_panel.csv.gz")
    for column in ("dt", "announcement_date", "nav_available_date"):
        panel[column] = pd.to_datetime(panel[column]).dt.normalize()
    panel = panel.sort_values("dt").reset_index(drop=True)
    spec = protocol["feature"]
    panel["premium_rate"] = panel["close_nav_deviation"]
    panel.loc[~panel["premium_feature_eligible"].astype(bool), "premium_rate"] = pd.NA
    lookback = int(spec["threshold_lookback_valid_sessions"])
    panel["prior_60_premium_q20"] = (
        panel["premium_rate"]
        .shift(1)
        .rolling(lookback, min_periods=lookback)
        .quantile(float(spec["threshold_quantile"]))
    )
    selected = panel.loc[
        panel["premium_rate"].le(panel["prior_60_premium_q20"])
        & panel["nav_available_date"].notna()
    ].copy()
    before_collision_count = int(len(selected))
    selected = selected.sort_values(["nav_available_date", "dt"]).drop_duplicates(
        "nav_available_date", keep="last"
    )
    events = selected.rename(columns={"dt": "signal_date", "nav_available_date": "event_date"})
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
            "announcement_date",
            "premium_rate",
            "prior_60_premium_q20",
        ]
    ].sort_values("event_date")

    calendar = pd.DatetimeIndex(panel["dt"])
    indicator = pd.Series(0, index=calendar, dtype=int)
    event_dates = pd.DatetimeIndex(events["event_date"])
    event_dates = event_dates.intersection(calendar)
    indicator.loc[event_dates] = 1
    rolling_sessions = int(protocol["density_gate"]["rolling_sessions"])
    rolling = indicator.rolling(rolling_sessions, min_periods=rolling_sessions).sum()
    rolling = rolling.loc[rolling.index >= pd.Timestamp(dataset["density_evaluation_start"])]
    if rolling.empty:
        raise ValueError("no full density evaluation windows")
    density = {
        "mechanism": protocol["research_target"]["mechanism"],
        "event_count_before_collision_control": before_collision_count,
        "event_count": int(len(events)),
        "collision_rows_removed": before_collision_count - int(len(events)),
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
    features = panel[
        [
            "dt",
            "announcement_date",
            "nav_available_date",
            "premium_rate",
            "prior_60_premium_q20",
            "premium_feature_eligible",
            "corporate_action",
        ]
    ].copy()
    for frame, columns in (
        (events, ["signal_date", "event_date", "announcement_date"]),
        (features, ["dt", "announcement_date", "nav_available_date"]),
    ):
        for column in columns:
            frame[column] = pd.to_datetime(frame[column]).dt.strftime("%Y-%m-%d")
    events.to_csv(artifacts / "mechanism_events.csv", index=False, lineterminator="\n")
    pd.DataFrame([density]).to_csv(
        artifacts / "density_metrics.csv", index=False, lineterminator="\n"
    )
    features.to_csv(
        artifacts / "discount_features.csv.gz",
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
        "# S003 EX42 执行\n\n"
        f"ETF折价修复事件密度门：`{status}`。共产生{density['event_count']}个事件，"
        f"合并同执行日冲突{density['collision_rows_removed']}条；滚动60日中位数"
        f"{density['rolling_60_median']:.1f}，第10百分位{density['rolling_60_p10']:.1f}，"
        f"最小值{density['rolling_60_min']:.1f}，最大值{density['rolling_60_max']:.1f}。\n\n"
        "本轮没有读取510500事件后收益。\n",
        encoding="utf-8",
    )
    conclusion = (
        "机制获得下一轮收益评价资格。"
        if density["density_eligible"]
        else "机制未达到三个月观察目标所需密度，停止该固定定义。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX42 结论\n\n"
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
