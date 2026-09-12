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


EXPERIMENT_ID = "20260913_S004_EX24"


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


def _sample_codes(snapshots: pd.DataFrame, per_group: int) -> dict[str, list[str]]:
    grouped = {
        pd.Timestamp(dt): set(group["con_code"].astype(str))
        for dt, group in snapshots.groupby("trade_date")
    }
    first = grouped[min(grouped)]
    last = grouped[max(grouped)]
    choices = {
        "persistent": sorted(first & last),
        "departed": sorted(first - last),
        "entered": sorted(last - first),
    }
    selected = {name: codes[:per_group] for name, codes in choices.items()}
    if any(len(codes) != per_group for codes in selected.values()):
        raise ValueError("not enough deterministic sample codes in a membership group")
    return selected


def _membership_by_session(
    snapshots: pd.DataFrame, calendar: pd.DatetimeIndex
) -> dict[pd.Timestamp, set[str]]:
    grouped = {
        pd.Timestamp(dt): set(group["con_code"].astype(str))
        for dt, group in snapshots.groupby("trade_date")
    }
    snapshot_dates = sorted(grouped)
    result: dict[pd.Timestamp, set[str]] = {}
    for session in calendar:
        known = [snapshot for snapshot in snapshot_dates if snapshot < session]
        if known:
            result[pd.Timestamp(session)] = grouped[known[-1]]
    return result


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
        else pd.DataFrame(columns=["ts_code", "trade_date", "suspend_timing"])
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
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = (
        "conditional_return_analysis",
        "breadth_feature_generation",
        "signal_generation",
        "parameter_selection",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX24 may only validate constituent data feasibility")

    dataset = protocol["dataset"]
    manifest = repo / dataset["calendar_manifest"]
    if _sha256(manifest) != dataset["calendar_manifest_sha256"]:
        raise ValueError("588080 calendar manifest differs from frozen evidence")
    start = pd.Timestamp(dataset["start"])
    cutoff = pd.Timestamp(dataset["development_cutoff"])
    fetch_start = pd.Timestamp(dataset["membership_fetch_start"])
    daily = load_market_data(repo / "data" / "raw", "588080.SH").daily
    calendar = pd.DatetimeIndex(pd.to_datetime(daily["dt"]).dt.normalize())
    calendar = calendar[(calendar >= start) & (calendar <= cutoff)]

    pro = get_tushare_pro(repo / ".env")
    snapshots = _fetch_memberships(
        pro, protocol["research_target"]["index_code"], fetch_start, cutoff
    )
    if snapshots.empty:
        raise ValueError("Tushare returned no historical index weights")
    snapshots["trade_date"] = pd.to_datetime(
        snapshots["trade_date"].astype(str), format="%Y%m%d"
    ).dt.normalize()
    snapshots = snapshots.sort_values(["trade_date", "con_code"]).reset_index(drop=True)

    sample = _sample_codes(snapshots, int(protocol["sample_rule"]["per_group"]))
    sample_daily, suspensions = _fetch_sample_data(pro, sample, start, cutoff)
    if sample_daily.empty:
        raise ValueError("Tushare returned no sampled constituent daily data")
    sample_daily["trade_date"] = pd.to_datetime(
        sample_daily["trade_date"].astype(str), format="%Y%m%d"
    ).dt.normalize()
    if not suspensions.empty:
        suspensions["trade_date"] = pd.to_datetime(
            suspensions["trade_date"].astype(str), format="%Y%m%d"
        ).dt.normalize()

    exchange_calendar = pro.trade_cal(
        exchange="SSE",
        start_date=_api_date(fetch_start),
        end_date=_api_date(cutoff),
        is_open="1",
        fields="cal_date",
    )
    open_sessions = set(pd.to_datetime(exchange_calendar["cal_date"]).dt.normalize())
    grouped = snapshots.groupby("trade_date")
    member_counts = grouped["con_code"].nunique()
    weight_sums = grouped["weight"].sum()
    completed_months = {
        period.strftime("%Y-%m")
        for period in pd.period_range(fetch_start, cutoff, freq="M")
        if period < cutoff.to_period("M")
    }
    observed_months = set(snapshots["trade_date"].dt.strftime("%Y-%m"))
    union = set(snapshots["con_code"].astype(str))
    first = set(grouped.get_group(snapshots["trade_date"].min())["con_code"].astype(str))
    last = set(grouped.get_group(snapshots["trade_date"].max())["con_code"].astype(str))
    memberships = _membership_by_session(snapshots, calendar)

    daily_keys = set(zip(sample_daily["trade_date"], sample_daily["ts_code"], strict=False))
    full_day_suspensions = (
        set(
            zip(
                suspensions.loc[suspensions["suspend_timing"].isna(), "trade_date"],
                suspensions.loc[suspensions["suspend_timing"].isna(), "ts_code"],
                strict=False,
            )
        )
        if not suspensions.empty
        else set()
    )
    sampled_codes = {code for codes in sample.values() for code in codes}
    expected_keys = {
        (session, code)
        for session, members in memberships.items()
        for code in sampled_codes.intersection(members)
    }
    missing_keys = expected_keys.difference(daily_keys)
    unexplained = sorted(missing_keys.difference(full_day_suspensions))
    bytes_per_row = _compressed_bytes(sample_daily) / max(len(sample_daily), 1)
    estimated_mb = bytes_per_row * len(memberships) * 50 / 1024 / 1024
    gate = protocol["quality_gate"]
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "first_snapshot": snapshots["trade_date"].min().strftime("%Y-%m-%d"),
        "last_snapshot": snapshots["trade_date"].max().strftime("%Y-%m-%d"),
        "snapshot_count": int(snapshots["trade_date"].nunique()),
        "missing_completed_months": sorted(completed_months.difference(observed_months)),
        "last_snapshot_age_days": int((cutoff - snapshots["trade_date"].max()).days),
        "member_count_min": int(member_counts.min()),
        "member_count_max": int(member_counts.max()),
        "weight_sum_min": float(weight_sums.min()),
        "weight_sum_max": float(weight_sums.max()),
        "nonpositive_weight_rows": int((snapshots["weight"] <= 0).sum()),
        "duplicate_snapshot_member_rows": int(
            snapshots.duplicated(["trade_date", "con_code"], keep=False).sum()
        ),
        "snapshot_dates_outside_exchange_calendar": [
            dt.strftime("%Y-%m-%d")
            for dt in sorted(set(snapshots["trade_date"]).difference(open_sessions))
        ],
        "historical_constituent_union": len(union),
        "first_last_overlap": len(first & last),
        "sample_codes": sample,
        "sample_daily_rows": int(len(sample_daily)),
        "sample_expected_member_day_rows": len(expected_keys),
        "sample_missing_daily_rows": len(missing_keys),
        "sample_explained_suspension_rows": len(missing_keys & full_day_suspensions),
        "sample_unexplained_missing_rows": [
            {"trade_date": dt.strftime("%Y-%m-%d"), "ts_code": code}
            for dt, code in unexplained
        ],
        "finite_observed_pct_change": bool(
            np.isfinite(sample_daily["pct_chg"].dropna().to_numpy(dtype=float)).all()
        ),
        "estimated_initial_daily_calls": len(union),
        "estimated_compressed_storage_mb": float(estimated_mb),
        "incremental_daily_calls": 1,
    }
    quality["passed"] = bool(
        not quality["missing_completed_months"]
        and quality["last_snapshot_age_days"] <= gate["maximum_last_snapshot_age_days"]
        and quality["member_count_min"] == gate["members_per_snapshot"]
        and quality["member_count_max"] == gate["members_per_snapshot"]
        and quality["weight_sum_min"] >= gate["weight_sum_min"]
        and quality["weight_sum_max"] <= gate["weight_sum_max"]
        and quality["nonpositive_weight_rows"] == 0
        and quality["duplicate_snapshot_member_rows"] == 0
        and not quality["snapshot_dates_outside_exchange_calendar"]
        and quality["first_last_overlap"] < gate["members_per_snapshot"]
        and not quality["sample_unexplained_missing_rows"]
        and quality["finite_observed_pct_change"]
        and quality["historical_constituent_union"] <= gate["historical_union_max"]
        and quality["estimated_compressed_storage_mb"]
        <= gate["estimated_compressed_storage_mb_max"]
        and quality["incremental_daily_calls"] <= gate["incremental_daily_calls_max"]
    )

    next_session = pd.Series(calendar, index=calendar).shift(-1)
    snapshots_out = snapshots.copy()
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
    sample_daily.to_csv(
        artifacts / "sample_constituent_daily.csv", index=False, lineterminator="\n"
    )
    suspensions.to_csv(
        artifacts / "sample_suspensions.csv", index=False, lineterminator="\n"
    )
    _write_json(artifacts / "data_quality.json", quality)

    status = "PASS" if quality["passed"] else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S004 EX24 执行\n\n"
        f"数据门：`{status}`。历史权重{quality['snapshot_count']}期，历史成分并集"
        f"{quality['historical_constituent_union']}只，每期成分数"
        f"{quality['member_count_min']}—{quality['member_count_max']}；完整月份缺失"
        f"{len(quality['missing_completed_months'])}个，抽样无法解释日线缺失"
        f"{len(quality['sample_unexplained_missing_rows'])}条。\n\n"
        f"估算首次日线调用{quality['estimated_initial_daily_calls']}次、压缩存储"
        f"{quality['estimated_compressed_storage_mb']:.1f}MB；日常增量日线调用1次、月度更新权重。"
        "本轮没有计算条件收益。\n",
        encoding="utf-8",
    )
    conclusion = (
        "历史时点成分广度数据满足下一轮完整面板建设要求。"
        if quality["passed"]
        else "历史时点成分广度未通过数据质量或OPC维护成本门，收益研究不得启动。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX24 结论\n\n"
        f"结论：`{status}`。{conclusion}本轮不构成策略有效性证据，没有创建候选或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S004",
            "symbol": "588080.SH",
            "development_cutoff": dataset["development_cutoff"],
            "status": status,
            "conditional_return_analysis": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
