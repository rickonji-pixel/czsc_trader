from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260911_S003_EX28"


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
    windows = []
    for period in pd.period_range(start, end, freq="M"):
        left = max(start, period.start_time.normalize())
        right = min(end, period.end_time.normalize())
        windows.append((left, right))
    return windows


def _api_date(value: pd.Timestamp) -> str:
    return value.strftime("%Y%m%d")


def _fetch_memberships(pro, index_code: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for left, right in _month_windows(start, end):
        frame = pro.index_weight(
            index_code=index_code,
            start_date=_api_date(left),
            end_date=_api_date(right),
        )
        if frame is not None and not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates().reset_index(drop=True)


def _membership_by_session(
    snapshots: pd.DataFrame, calendar: pd.DatetimeIndex
) -> dict[pd.Timestamp, set[str]]:
    available: dict[pd.Timestamp, set[str]] = {}
    grouped = {
        pd.Timestamp(dt): set(group["con_code"].astype(str))
        for dt, group in snapshots.groupby("trade_date")
    }
    snapshot_dates = sorted(grouped)
    for dt in calendar:
        known = [snapshot for snapshot in snapshot_dates if snapshot < dt]
        if known:
            available[pd.Timestamp(dt)] = grouped[known[-1]]
    return available


def _sample_codes(snapshots: pd.DataFrame, count: int) -> dict[str, list[str]]:
    grouped = {
        pd.Timestamp(dt): set(group["con_code"].astype(str))
        for dt, group in snapshots.groupby("trade_date")
    }
    first = grouped[min(grouped)]
    last = grouped[max(grouped)]
    groups = {
        "persistent": sorted(first & last),
        "departed": sorted(first - last),
        "entered": sorted(last - first),
    }
    selected = {name: codes[:count] for name, codes in groups.items()}
    if any(len(codes) != count for codes in selected.values()):
        raise ValueError("not enough deterministic sample codes in one membership group")
    return selected


def _fetch_sample_data(
    pro,
    sample: dict[str, list[str]],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    daily_frames: list[pd.DataFrame] = []
    suspension_frames: list[pd.DataFrame] = []
    for code in sorted({code for codes in sample.values() for code in codes}):
        daily = pro.daily(
            ts_code=code,
            start_date=_api_date(start),
            end_date=_api_date(end),
            fields="ts_code,trade_date,close,pre_close,pct_chg,vol,amount",
        )
        if daily is not None and not daily.empty:
            daily_frames.append(daily)
        suspension = pro.suspend_d(
            ts_code=code,
            start_date=_api_date(start),
            end_date=_api_date(end),
            suspend_type="S",
        )
        if suspension is not None and not suspension.empty:
            suspension_frames.append(suspension)
    daily = pd.concat(daily_frames, ignore_index=True) if daily_frames else pd.DataFrame()
    suspensions = (
        pd.concat(suspension_frames, ignore_index=True)
        if suspension_frames
        else pd.DataFrame(columns=["ts_code", "trade_date", "suspend_type", "suspend_timing"])
    )
    return daily, suspensions


def _compressed_bytes(frame: pd.DataFrame) -> int:
    raw = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=9, mtime=0) as archive:
        archive.write(raw)
    return len(buffer.getvalue())


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
            "breadth_feature_generation",
            "signal_generation",
            "parameter_selection",
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX28 may only validate point-in-time breadth data feasibility")

    dataset = protocol["dataset"]
    manifest = repo_root / dataset["calendar_manifest"]
    if _sha256(manifest) != dataset["calendar_manifest_sha256"]:
        raise ValueError("510500 calendar manifest differs from frozen evidence")
    start = pd.Timestamp(dataset["start"])
    end = pd.Timestamp(dataset["development_cutoff"])
    daily_510500 = load_market_data(repo_root / "data" / "raw", "510500.SH").daily
    calendar = pd.DatetimeIndex(pd.to_datetime(daily_510500["dt"]).dt.normalize())
    calendar = calendar[(calendar >= start) & (calendar <= end)]

    pro = get_tushare_pro(repo_root / ".env")
    index_code = protocol["research_target"]["index_code"]
    snapshots = _fetch_memberships(pro, index_code, start, end)
    if snapshots.empty:
        raise ValueError("Tushare returned no historical index weights")
    snapshots["trade_date"] = pd.to_datetime(
        snapshots["trade_date"].astype(str), format="%Y%m%d"
    ).dt.normalize()
    snapshots = snapshots.sort_values(["trade_date", "con_code"]).reset_index(drop=True)

    sample = _sample_codes(snapshots, int(protocol["sample_rule"]["per_group"]))
    sample_daily, suspensions = _fetch_sample_data(pro, sample, start, end)
    if sample_daily.empty:
        raise ValueError("Tushare returned no sampled constituent daily data")
    sample_daily["trade_date"] = pd.to_datetime(
        sample_daily["trade_date"].astype(str), format="%Y%m%d"
    ).dt.normalize()
    if not suspensions.empty:
        suspensions["trade_date"] = pd.to_datetime(
            suspensions["trade_date"].astype(str), format="%Y%m%d"
        ).dt.normalize()

    grouped = snapshots.groupby("trade_date")
    member_counts = grouped["con_code"].nunique()
    weight_sums = grouped["weight"].sum()
    expected_months = {period.strftime("%Y-%m") for period in pd.period_range(start, end, freq="M")}
    observed_months = set(snapshots["trade_date"].dt.strftime("%Y-%m"))
    union = set(snapshots["con_code"].astype(str))
    first_members = set(grouped.get_group(snapshots["trade_date"].min())["con_code"].astype(str))
    last_members = set(grouped.get_group(snapshots["trade_date"].max())["con_code"].astype(str))
    duplicate_rows = int(snapshots.duplicated(["trade_date", "con_code"], keep=False).sum())

    memberships = _membership_by_session(snapshots, calendar)
    daily_keys = set(zip(sample_daily["trade_date"], sample_daily["ts_code"], strict=False))
    full_day_suspensions = set(
        zip(
            suspensions.loc[suspensions.get("suspend_timing").isna(), "trade_date"],
            suspensions.loc[suspensions.get("suspend_timing").isna(), "ts_code"],
            strict=False,
        )
    ) if not suspensions.empty else set()
    sampled_codes = {code for codes in sample.values() for code in codes}
    expected_keys = {
        (dt, code)
        for dt, members in memberships.items()
        for code in sampled_codes.intersection(members)
    }
    missing_keys = expected_keys.difference(daily_keys)
    unexplained = sorted(missing_keys.difference(full_day_suspensions))
    sample_daily = sample_daily.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    finite_pct_change = bool(
        np.isfinite(sample_daily["pct_chg"].dropna().to_numpy(dtype=float)).all()
    )

    expected_full_rows = len(memberships) * 500
    bytes_per_sample_row = _compressed_bytes(sample_daily) / max(len(sample_daily), 1)
    estimated_storage_mb = bytes_per_sample_row * expected_full_rows / 1024 / 1024
    cost_gate = protocol["opc_cost_gate"]
    quality_gate = protocol["quality_gate"]
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "first_snapshot": snapshots["trade_date"].min().strftime("%Y-%m-%d"),
        "last_snapshot": snapshots["trade_date"].max().strftime("%Y-%m-%d"),
        "snapshot_count": int(snapshots["trade_date"].nunique()),
        "missing_months": sorted(expected_months.difference(observed_months)),
        "member_count_min": int(member_counts.min()),
        "member_count_max": int(member_counts.max()),
        "weight_sum_min": float(weight_sums.min()),
        "weight_sum_max": float(weight_sums.max()),
        "nonpositive_weight_rows": int((snapshots["weight"] <= 0).sum()),
        "duplicate_snapshot_member_rows": duplicate_rows,
        "snapshot_dates_outside_master_calendar": [
            dt.strftime("%Y-%m-%d")
            for dt in pd.DatetimeIndex(snapshots["trade_date"].unique()).difference(calendar)
        ],
        "historical_constituent_union": len(union),
        "first_last_overlap": len(first_members & last_members),
        "sample_codes": sample,
        "sample_daily_rows": int(len(sample_daily)),
        "sample_expected_member_day_rows": len(expected_keys),
        "sample_missing_daily_rows": len(missing_keys),
        "sample_explained_suspension_rows": len(missing_keys & full_day_suspensions),
        "sample_unexplained_missing_rows": [
            {"trade_date": dt.strftime("%Y-%m-%d"), "ts_code": code}
            for dt, code in unexplained
        ],
        "finite_observed_pct_change": finite_pct_change,
        "estimated_initial_daily_calls": len(union),
        "estimated_compressed_storage_mb": float(estimated_storage_mb),
        "incremental_daily_calls": int(cost_gate["incremental_daily_calls"]),
    }
    quality["passed"] = bool(
        not quality["missing_months"]
        and quality["member_count_min"] == quality_gate["members_per_snapshot"]
        and quality["member_count_max"] == quality_gate["members_per_snapshot"]
        and quality["weight_sum_min"] >= quality_gate["weight_sum_min"]
        and quality["weight_sum_max"] <= quality_gate["weight_sum_max"]
        and quality["nonpositive_weight_rows"] == 0
        and duplicate_rows == 0
        and not quality["snapshot_dates_outside_master_calendar"]
        and len(union) > quality_gate["historical_union_min_exclusive"]
        and first_members != last_members
        and not unexplained
        and finite_pct_change
        and len(union) <= cost_gate["historical_union_max"]
        and len(union) <= cost_gate["initial_daily_calls_max"]
        and estimated_storage_mb <= cost_gate["estimated_compressed_storage_mb_max"]
    )

    snapshots_out = snapshots.copy()
    next_session = pd.Series(calendar, index=calendar).shift(-1)
    snapshots_out["effective_from"] = snapshots_out["trade_date"].map(next_session)
    for frame in (snapshots_out, sample_daily, suspensions):
        if "trade_date" in frame:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.strftime("%Y-%m-%d")
    snapshots_out["effective_from"] = pd.to_datetime(
        snapshots_out["effective_from"]
    ).dt.strftime("%Y-%m-%d")
    snapshots_out.to_csv(
        artifacts / "index_weight_snapshots.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    sample_daily.to_csv(artifacts / "sample_constituent_daily.csv", index=False, lineterminator="\n")
    suspensions.to_csv(artifacts / "sample_suspensions.csv", index=False, lineterminator="\n")
    _write_json(artifacts / "data_quality.json", quality)

    status = "PASS" if quality["passed"] else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S003 EX28 执行\n\n"
        f"数据门结果：`{status}`。取得{quality['snapshot_count']}个历史权重快照，历史成分并集"
        f"{quality['historical_constituent_union']}只，单快照成分数范围"
        f"{quality['member_count_min']}—{quality['member_count_max']}，缺失月份"
        f"{len(quality['missing_months'])}个。抽样日线缺失{quality['sample_missing_daily_rows']}条，"
        f"其中停牌解释{quality['sample_explained_suspension_rows']}条、无法解释"
        f"{len(quality['sample_unexplained_missing_rows'])}条。\n\n"
        f"估算首次回填调用{quality['estimated_initial_daily_calls']}次，完整压缩存储约"
        f"{quality['estimated_compressed_storage_mb']:.1f}MB，日常增量日线调用1次。"
        "本轮没有计算宽度或条件收益。\n",
        encoding="utf-8",
    )
    conclusion = (
        "历史时点成分宽度的数据与维护成本满足下一轮特征定义要求。"
        if quality["passed"]
        else "历史时点成分宽度未通过数据或OPC维护成本门，当前不得进入收益研究。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX28 结论\n\n"
        f"结论：`{status}`。{conclusion}该结论不构成策略有效性证据，也没有创建候选或修改SM/PTE。\n",
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
            "conditional_return_analysis": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
