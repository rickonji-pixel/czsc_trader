from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


EXPERIMENT_ID = "20260913_S005_EX44"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    sys.path.insert(0, str(repo / "src"))
    from czsc_trader.experiment_archive import (  # noqa: PLC0415
        build_experiment_manifest,
        validate_experiment_archive,
    )
    from czsc_trader.identity import raw_file_sha256  # noqa: PLC0415

    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("reads_prices_or_returns"):
        raise ValueError("EX44 may not read prices or returns")
    for spec in protocol["inputs"].values():
        path = repo / str(spec["path"])
        if raw_file_sha256(path) != spec["sha256"]:
            raise ValueError(f"input differs from frozen protocol: {path}")

    membership_spec = protocol["inputs"]["membership"]
    membership = pd.read_csv(
        repo / str(membership_spec["path"]),
        usecols=["dt", "con_code", "weight"],
        parse_dates=["dt"],
    )
    membership["dt"] = membership["dt"].dt.normalize()
    calendar = np.sort(membership["dt"].unique())

    def next_session(value: pd.Timestamp) -> pd.Timestamp:
        position = int(np.searchsorted(calendar, value.to_datetime64(), side="right"))
        return pd.Timestamp(calendar[position]) if position < len(calendar) else pd.NaT

    frames: list[pd.DataFrame] = []
    fields = {
        "forecast": "p_change_mid",
        "express": "yoy_net_profit",
        "fina_indicator": "netprofit_yoy",
    }
    for source in protocol["source_priority"]:
        spec = protocol["inputs"][source]
        value = pd.read_csv(repo / str(spec["path"]), dtype={"ts_code": str})
        value["ann_date"] = pd.to_datetime(value["ann_date"], format="%Y%m%d")
        value["end_date"] = pd.to_datetime(value["end_date"], format="%Y%m%d")
        field = fields[source]
        if source == "forecast":
            bounds = value[["p_change_min", "p_change_max"]].apply(
                pd.to_numeric, errors="coerce"
            )
            value[field] = bounds.mean(axis=1)
            value.loc[bounds.isna().all(axis=1), field] = np.nan
        else:
            value[field] = pd.to_numeric(value[field], errors="coerce")
        value = value.rename(columns={field: "profit_growth_yoy"})
        value["source"] = source
        frames.append(
            value[["ts_code", "ann_date", "end_date", "source", "profit_growth_yoy"]]
        )

    priority = {source: index for index, source in enumerate(protocol["source_priority"])}
    raw = pd.concat(frames, ignore_index=True)
    raw = raw[raw["profit_growth_yoy"].notna()].copy()
    raw["priority"] = raw["source"].map(priority)
    first = (
        raw.sort_values(["ts_code", "end_date", "ann_date", "priority"])
        .drop_duplicates(["ts_code", "end_date"], keep="first")
        .copy()
    )
    prior = first[["ts_code", "end_date", "profit_growth_yoy"]].copy()
    prior["end_date"] = prior["end_date"] + pd.DateOffset(years=1)
    prior = prior.rename(columns={"profit_growth_yoy": "prior_year_profit_growth_yoy"})
    events = first.merge(prior, on=["ts_code", "end_date"], how="left", validate="one_to_one")
    events["acceleration"] = events["profit_growth_yoy"] - events["prior_year_profit_growth_yoy"]
    events["eligible_session"] = events["ann_date"].map(next_session)
    events = events.merge(
        membership,
        left_on=["eligible_session", "ts_code"],
        right_on=["dt", "con_code"],
        how="inner",
        validate="many_to_one",
    )
    events = events[events["acceleration"].notna()].copy()
    events["positive_weight"] = events["weight"].where(events["acceleration"] > 0, 0.0)
    events["negative_weight"] = events["weight"].where(events["acceleration"] < 0, 0.0)
    daily = events.groupby("eligible_session", as_index=False).agg(
        disclosures=("ts_code", "nunique"),
        positive_weight=("positive_weight", "sum"),
        negative_weight=("negative_weight", "sum"),
    )
    daily["net_breadth"] = daily["positive_weight"] - daily["negative_weight"]
    daily["direction"] = np.sign(daily["net_breadth"]).astype(int)

    positive_dates = set(daily.loc[daily["direction"] > 0, "eligible_session"])
    sessions = pd.DataFrame({"dt": pd.to_datetime(calendar)})
    sessions["positive_event"] = sessions["dt"].isin(positive_dates).astype(int)
    window = int(protocol["density_gate"]["rolling_sessions"])
    sessions["positive_events_rolling"] = sessions["positive_event"].rolling(
        window, min_periods=window
    ).sum()
    evaluated = sessions.loc[
        sessions["dt"] >= pd.Timestamp(protocol["density_gate"]["evaluation_start"]),
        "positive_events_rolling",
    ]
    median = float(evaluated.median())
    p10 = float(evaluated.quantile(0.1))
    gate = protocol["density_gate"]
    passed = bool(
        median >= float(gate["minimum_median_positive_days"])
        and p10 >= float(gate["minimum_p10_positive_days"])
    )
    annual = {
        str(int(year)): int(count)
        for year, count in daily[daily["direction"] > 0]
        .groupby(daily.loc[daily["direction"] > 0, "eligible_session"].dt.year)
        .size()
        .items()
    }
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "first_usable_disclosures": int(len(first)),
        "point_in_time_events_with_acceleration": int(len(events)),
        "disclosure_event_days": int(len(daily)),
        "positive_event_days": int(len(positive_dates)),
        "negative_event_days": int((daily["direction"] < 0).sum()),
        "positive_event_days_by_year": annual,
        "rolling_60_positive_days_median": median,
        "rolling_60_positive_days_p10": p10,
        "reads_prices_or_returns": False,
        "passed": passed,
    }
    _write(artifacts / "density_quality.json", quality)
    events_out = events[
        [
            "ts_code", "ann_date", "end_date", "source", "eligible_session",
            "weight", "profit_growth_yoy", "prior_year_profit_growth_yoy", "acceleration",
        ]
    ].copy()
    for column in ("ann_date", "end_date", "eligible_session"):
        events_out[column] = pd.to_datetime(events_out[column]).dt.strftime("%Y-%m-%d")
    events_out.to_csv(artifacts / "acceleration_events.csv", index=False, lineterminator="\n")
    daily_out = daily.copy()
    daily_out["eligible_session"] = daily_out["eligible_session"].dt.strftime("%Y-%m-%d")
    daily_out.to_csv(artifacts / "daily_breadth.csv", index=False, lineterminator="\n")

    status = "PASS" if passed else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S005 EX44 执行\n\n"
        f"密度门结果：`{status}`。形成{len(events)}条满足历史成员关系且具有同比加速度的公司披露，"
        f"聚合为{len(daily)}个披露日，其中正向{len(positive_dates)}日、负向{int((daily['direction'] < 0).sum())}日。\n\n"
        f"正向披露日滚动60交易日中位数为{median:.1f}，P10为{p10:.1f}；"
        f"年度分布为{annual}。本轮没有读取价格或收益。\n",
        encoding="utf-8",
    )
    decision = "PROCEED_TO_RETURN_TEST" if passed else "STOP_AS_STANDALONE_S005_MECHANISM"
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX44 结论\n\n"
        f"裁决：`{decision}`。"
        + (
            "信号密度达到S005前瞻证据速度门，可进入固定持有期的收益检验。"
            if passed
            else "财报披露具有明显季节性，无法持续满足S005的60交易日证据速度要求。保留为未来组合增强因子；当前不读取收益、不继续把它开发为独立候选。"
        )
        + "没有创建候选、冻结策略或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S005",
            "symbol": "588080.SH",
            "development_cutoff": str(protocol["development_cutoff"]),
            "status": status,
            "decision": decision,
            "reads_prices_or_returns": False,
            "candidate_created": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
