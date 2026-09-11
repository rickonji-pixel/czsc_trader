from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260911_S003_EX49"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _month_windows(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    return [
        (max(start, period.start_time.normalize()), min(end, period.end_time.normalize()))
        for period in pd.period_range(start, end, freq="M")
    ]


def _request(function, **kwargs) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            value = function(**kwargs)
            return pd.DataFrame() if value is None else value
        except Exception as exc:  # Provider exposes transport and quota failures generically.
            last_error = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"provider request failed: {kwargs}") from last_error


def _build_membership(snapshots: pd.DataFrame, calendar: pd.DatetimeIndex) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    dates = sorted(pd.DatetimeIndex(snapshots["trade_date"].unique()))
    for index, snapshot_date in enumerate(dates):
        next_snapshot = dates[index + 1] if index + 1 < len(dates) else None
        sessions = calendar[calendar > snapshot_date]
        if next_snapshot is not None:
            sessions = sessions[sessions <= next_snapshot]
        if sessions.empty:
            continue
        members = snapshots.loc[
            snapshots["trade_date"].eq(snapshot_date), ["con_code", "weight"]
        ].copy()
        repeated = members.loc[members.index.repeat(len(sessions))].reset_index(drop=True)
        repeated["dt"] = np.tile(sessions.to_numpy(), len(members))
        repeated["snapshot_date"] = snapshot_date
        frames.append(repeated)
    if not frames:
        return pd.DataFrame(columns=["dt", "con_code", "weight", "snapshot_date"])
    return pd.concat(frames, ignore_index=True).sort_values(["dt", "con_code"]).reset_index(drop=True)


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = (
        "breadth_feature_generation",
        "event_generation",
        "conditional_return_analysis",
        "parameter_selection",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX49 may only validate point-in-time moneyflow data")

    dataset = protocol["dataset"]
    source_experiment = repo / "experiments" / str(dataset["calendar_experiment"])
    expected = {
        source_experiment / "experiment_manifest.json": dataset["calendar_manifest_sha256"],
        source_experiment / "artifacts" / "data_quality.json": dataset["calendar_quality_sha256"],
        source_experiment / "artifacts" / "512100_daily.csv": dataset["calendar_daily_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source digest differs: {path}")
    if not _read_json(source_experiment / "artifacts" / "data_quality.json").get("passed"):
        raise ValueError("EX48 data gate did not pass")
    daily = pd.read_csv(source_experiment / "artifacts" / "512100_daily.csv")
    calendar = pd.DatetimeIndex(pd.to_datetime(daily["datetime"]).dt.normalize().sort_values().unique())

    pro = get_tushare_pro(repo / ".env")
    index_code = str(protocol["research_target"]["index_code"])
    query_start = pd.Timestamp(dataset["membership_query_start"])
    cutoff = pd.Timestamp(dataset["development_cutoff"])
    snapshot_frames: list[pd.DataFrame] = []
    membership_calls = 0
    for left, right in _month_windows(query_start, cutoff):
        frame = _request(
            pro.index_weight,
            index_code=index_code,
            start_date=left.strftime("%Y%m%d"),
            end_date=right.strftime("%Y%m%d"),
        )
        membership_calls += 1
        if not frame.empty:
            snapshot_frames.append(frame)
    if not snapshot_frames:
        raise ValueError("index_weight returned no snapshots")
    snapshots = pd.concat(snapshot_frames, ignore_index=True).drop_duplicates().reset_index(drop=True)
    snapshots["trade_date"] = pd.to_datetime(snapshots["trade_date"].astype(str), format="%Y%m%d")
    snapshots["weight"] = pd.to_numeric(snapshots["weight"], errors="coerce")
    membership = _build_membership(snapshots, calendar)
    if membership.empty or membership.duplicated(["dt", "con_code"]).any():
        raise ValueError("invalid point-in-time membership panel")

    fields = ",".join(protocol["source"]["moneyflow_fields"])
    flow_frames: list[pd.DataFrame] = []
    moneyflow_calls = 0
    for index, dt in enumerate(calendar, start=1):
        frame = _request(pro.moneyflow, trade_date=dt.strftime("%Y%m%d"), fields=fields)
        moneyflow_calls += 1
        if not frame.empty:
            members = set(membership.loc[membership["dt"].eq(dt), "con_code"])
            scoped = frame.loc[frame["ts_code"].isin(members)].copy()
            if not scoped.empty:
                scoped["dt"] = dt
                flow_frames.append(scoped[["dt", "ts_code", "net_mf_amount"]])
        if index % 50 == 0 or index == len(calendar):
            print(f"moneyflow sessions {index}/{len(calendar)}", flush=True)
        time.sleep(0.13)
    flow = pd.concat(flow_frames, ignore_index=True) if flow_frames else pd.DataFrame(
        columns=["dt", "ts_code", "net_mf_amount"]
    )
    flow["net_mf_amount"] = pd.to_numeric(flow["net_mf_amount"], errors="coerce")
    duplicate_flow_rows = int(flow.duplicated(["dt", "ts_code"], keep=False).sum())
    merged = membership.merge(
        flow,
        left_on=["dt", "con_code"],
        right_on=["dt", "ts_code"],
        how="left",
        validate="one_to_one",
    )
    merged["observed_moneyflow"] = merged["net_mf_amount"].notna()
    merged["observed_weight"] = merged["weight"].where(merged["observed_moneyflow"], 0.0)
    daily_coverage = merged.groupby("dt", sort=True, observed=True).agg(
        observed_members=("observed_moneyflow", "sum"),
        expected_members=("con_code", "size"),
        observed_weight=("observed_weight", "sum"),
        total_weight=("weight", "sum"),
    )
    daily_coverage["member_coverage_ratio"] = (
        daily_coverage["observed_members"] / daily_coverage["expected_members"]
    )
    daily_coverage["observed_weight_ratio"] = (
        daily_coverage["observed_weight"] / daily_coverage["total_weight"]
    )
    snapshot_quality = snapshots.groupby("trade_date", sort=True).agg(
        members=("con_code", "nunique"),
        rows=("con_code", "size"),
        weight_sum=("weight", "sum"),
        weight_min=("weight", "min"),
    )
    completed_months = {
        period.strftime("%Y-%m")
        for period in pd.period_range(query_start, cutoff, freq="M")
        if period < cutoff.to_period("M")
    }
    observed_months = set(snapshots["trade_date"].dt.strftime("%Y-%m"))
    missing_completed_months = sorted(completed_months.difference(observed_months))
    finite = bool(
        np.isfinite(merged.loc[merged["observed_moneyflow"], "net_mf_amount"].to_numpy()).all()
    )
    total_calls = membership_calls + moneyflow_calls
    gate = protocol["quality_gate"]
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "access_available": True,
        "snapshot_count": int(len(snapshot_quality)),
        "missing_completed_months": missing_completed_months,
        "snapshot_member_count_min": int(snapshot_quality["members"].min()),
        "snapshot_member_count_max": int(snapshot_quality["members"].max()),
        "snapshot_duplicate_members": int((snapshot_quality["rows"] - snapshot_quality["members"]).sum()),
        "snapshot_weight_sum_min": float(snapshot_quality["weight_sum"].min()),
        "snapshot_weight_sum_max": float(snapshot_quality["weight_sum"].max()),
        "snapshot_weight_min": float(snapshot_quality["weight_min"].min()),
        "historical_constituent_union": int(snapshots["con_code"].nunique()),
        "causal_membership": bool((membership["snapshot_date"] < membership["dt"]).all()),
        "expected_member_sessions": int(len(merged)),
        "observed_member_sessions": int(merged["observed_moneyflow"].sum()),
        "overall_member_coverage_ratio": float(merged["observed_moneyflow"].mean()),
        "daily_member_coverage_p10": float(daily_coverage["member_coverage_ratio"].quantile(0.10)),
        "daily_member_coverage_min": float(daily_coverage["member_coverage_ratio"].min()),
        "daily_observed_weight_p10": float(daily_coverage["observed_weight_ratio"].quantile(0.10)),
        "daily_observed_weight_min": float(daily_coverage["observed_weight_ratio"].min()),
        "first_session": daily_coverage.index.min().strftime("%Y-%m-%d"),
        "last_session": daily_coverage.index.max().strftime("%Y-%m-%d"),
        "duplicate_moneyflow_rows": duplicate_flow_rows,
        "finite_observed_net_mf_amount": finite,
        "membership_api_calls": membership_calls,
        "moneyflow_api_calls": moneyflow_calls,
        "total_api_calls": total_calls,
        "daily_incremental_calls": 1,
        "filled_missing_rows": 0,
        "conditional_return_analysis": False,
    }
    quality["passed"] = bool(
        not missing_completed_months
        and quality["snapshot_member_count_min"] == int(gate["members_per_snapshot"])
        and quality["snapshot_member_count_max"] == int(gate["members_per_snapshot"])
        and quality["snapshot_duplicate_members"] == 0
        and quality["snapshot_weight_sum_min"] >= float(gate["weight_sum_min"])
        and quality["snapshot_weight_sum_max"] <= float(gate["weight_sum_max"])
        and quality["snapshot_weight_min"] > 0
        and quality["historical_constituent_union"] > int(gate["historical_union_min_exclusive"])
        and quality["causal_membership"]
        and quality["overall_member_coverage_ratio"] >= float(gate["minimum_overall_member_coverage_ratio"])
        and quality["daily_member_coverage_p10"] >= float(gate["minimum_daily_member_coverage_p10"])
        and quality["daily_observed_weight_p10"] >= float(gate["minimum_daily_observed_weight_p10"])
        and quality["daily_observed_weight_min"] >= float(gate["minimum_daily_observed_weight_min"])
        and quality["first_session"] == str(dataset["start"])
        and quality["last_session"] == str(dataset["development_cutoff"])
        and quality["duplicate_moneyflow_rows"] == 0
        and finite
        and total_calls <= int(gate["maximum_total_api_calls"])
        and quality["daily_incremental_calls"] <= int(gate["maximum_daily_incremental_calls"])
    )

    output = merged.drop(columns="ts_code").copy()
    output["dt"] = output["dt"].dt.strftime("%Y-%m-%d")
    output["snapshot_date"] = output["snapshot_date"].dt.strftime("%Y-%m-%d")
    output.to_csv(
        artifacts / "constituent_moneyflow_panel.csv.gz",
        index=False,
        encoding="utf-8-sig",
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    snapshots.assign(trade_date=snapshots["trade_date"].dt.strftime("%Y-%m-%d")).to_csv(
        artifacts / "index_weight_snapshots.csv.gz",
        index=False,
        encoding="utf-8-sig",
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    daily_coverage.reset_index().assign(
        dt=lambda value: value["dt"].dt.strftime("%Y-%m-%d")
    ).to_csv(artifacts / "daily_coverage.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    _write_json(artifacts / "data_quality.json", quality)
    status = "PASS" if quality["passed"] else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S003 EX49 执行\n\n"
        f"数据门：`{status}`。{quality['snapshot_count']}期权重快照、历史并集"
        f"{quality['historical_constituent_union']}只；成员日期{quality['expected_member_sessions']:,}条，"
        f"资金流覆盖{quality['observed_member_sessions']:,}条（{quality['overall_member_coverage_ratio']:.2%}）。"
        f"单日成员覆盖率P10为{quality['daily_member_coverage_p10']:.2%}，"
        f"已观测权重P10为{quality['daily_observed_weight_p10']:.2%}、最小"
        f"{quality['daily_observed_weight_min']:.2%}；首次建设调用{total_calls}次。\n\n"
        "缺失值未填充，本轮没有计算宽度、事件或条件收益。\n",
        encoding="utf-8",
    )
    conclusion = (
        "中证1000历史时点成分资金流满足固定规则横截面复现要求。"
        if quality["passed"]
        else "数据未通过冻结质量门，横截面复现停止。"
    )
    (experiment / "04_conclusion.md").write_text(
        f"# S003 EX49 结论\n\n结论：`{status}`。{conclusion}本轮没有创建候选或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": protocol["research_target"]["trade_symbol"],
            "development_cutoff": dataset["development_cutoff"],
            "status": status,
            "conditional_return_analysis": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
